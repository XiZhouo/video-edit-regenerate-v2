"""Adversarial contract tests for the pure SQLite business-snapshot boundary.

The contract under test is deliberately narrower than persistence itself.  It
captures one complete in-memory business head into immutable SQL-shaped rows,
verifies every byte/column/root binding, and restores a detached Document.  It
must not choose an authority, touch SQLite/files, or reuse the permissive legacy
``Document.open`` / ``Entity.from_dict`` loading paths.
"""

from __future__ import annotations

import builtins
import copy
import dataclasses
import hashlib
import sqlite3
import sys
from collections import defaultdict
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, Callable

import pytest


_HERE = Path(__file__).resolve().parent
_DATA_STRUCTURE = _HERE.parents[1] / "01-data-structure"
_DOCUMENT_DIR = _DATA_STRUCTURE / "01-document"
_ENTITY_DIR = _DATA_STRUCTURE / "02-entity"
_FIELD_TYPES_DIR = _DATA_STRUCTURE / "04-field-types"
for _directory in (_HERE, _DOCUMENT_DIR, _ENTITY_DIR, _FIELD_TYPES_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import business_snapshot as snapshot_module  # noqa: E402
from business_codec import (  # noqa: E402
    BusinessCodecError,
    CODEC_ID,
    CODEC_VERSION,
    decode_payload,
    encode_payload,
    payload_sha256,
)
from business_snapshot import (  # noqa: E402
    BusinessSnapshotError,
    BusinessSnapshotSet,
    CaptureResult,
    DocumentSnapshotRow,
    EntityKey,
    EntitySnapshotRow,
    ProjectSnapshotRow,
    RestoredBusinessHead,
    capture_business_snapshot,
    restore_business_snapshot,
    verify_business_snapshot,
)
from document import Document, ReadOnlySourceSpec, ZeError  # noqa: E402
from entity import Actor, AiProvenance, Entity, _no_op_validator  # noqa: E402
from field_types import make_validator  # noqa: E402


PROJECT_SCHEMA = "ze.business-snapshot.project.v1"
DOCUMENT_SCHEMA = "ze.business-snapshot.document.v1"
ENTITY_SCHEMA = "ze.business-snapshot.entity.v1"
ENTITY_SET_HASH_DOMAIN = "ze.business-snapshot.entity-set.v1"
SQLITE_INT_MAX = 2**63 - 1


def _assert_snapshot_error(call: Callable[[], Any]) -> BusinessSnapshotError:
    with pytest.raises(BusinessSnapshotError) as raised:
        call()
    return raised.value


def _assert_clean_exception_boundary(
    error: BusinessSnapshotError,
    forbidden_marker: str,
) -> None:
    """The translated error must not retain the lower exception or echo data."""
    assert error.__cause__ is None
    assert error.__context__ is None
    assert set(error.__dict__) == {"code", "path"}
    assert forbidden_marker not in str(error)
    assert forbidden_marker not in repr(error)
    assert all(forbidden_marker not in repr(value) for value in error.args)


def _make_document(*, include_read_only: bool = True) -> Document:
    data_schema = {
        "asset": {
            "type": "object",
            "properties": {"sha": {"type": "string"}},
        },
        "card": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "score": {"type": "number"},
            },
        },
    }
    document = Document(
        id="document/root",
        name="主文档",
        schema_version="3.7.0",
        collaboration_mode="live_collab",
        save_policy="checkpoint_commit",
        schema=data_schema,
        config={"viewport": {"width": 1920, "height": 1080}, "flags": [True]},
        acl={
            "owner": "owner-1",
            "collaborators": ["user-2", "user-3"],
            "extension": {"reviewers": ["user-4"]},
        },
        # Runtime declarations are intentionally not business snapshot state.
        harvest_rules=[{"type": "card", "where": {"active": True}}],
    )
    document._v = 8
    document._created_at = 1
    document._updated_at = 20.5

    # Insert in deliberately non-canonical order.  Capture owns ordering.
    card_z = Entity(
        type="card",
        id="z-card",
        name="Z card",
        data={"title": "Zulu", "score": 9.5},
    )
    document.add(card_z)
    card_z._version = 4
    card_z._created_at = 2
    card_z._updated_at = 9.25
    card_z._provenance = {
        "title": AiProvenance(
            actor="user-1",
            model="model-x",
            model_version="2026-07",
            prompt_template_id="card-title-v2",
            prompt_args={"tone": "short", "references": ["a", "b"]},
            # Both numeric JSON types are intentionally retained exactly.
            cost_usd=1,
            token_in=12,
            token_out=7,
            ts=3.5,
            trace_id="trace-1",
        ),
    }
    card_z._actor_log = [
        {
            "version": 2,
            "actor": {"user_id": "u-ai", "device_id": "d-ai", "source": "ai"},
            "ts": 3,
            "fields": ["title", "score"],
        },
        {
            "version": 4,
            "actor": {"user_id": "u-sync", "device_id": "d-sync", "source": "sync"},
            "ts": 9.25,
            # Order is history, not a set; the snapshot must not normalize it.
            "fields": ["z", "a"],
        },
    ]

    card_a = Entity(
        type="card",
        id="a-card",
        name="Archived",
        data={"title": "Alpha", "score": 1},
    )
    document.add(card_a)
    card_a._version = 2
    card_a._deleted = True
    card_a._created_at = 4
    card_a._updated_at = 6
    card_a._actor_log = [
        {
            "version": 2,
            "actor": {"user_id": "u1", "device_id": "d1", "source": "user"},
            "ts": 6,
            "fields": ["__deleted__"],
        },
    ]

    asset = Entity(
        type="asset",
        id="middle-asset",
        name="Asset",
        data={"sha": "0" * 64},
    )
    document.add(asset)
    asset._created_at = 7
    asset._updated_at = 7

    if include_read_only:
        document.register_read_only_source(
            ReadOnlySourceSpec(
                name="catalog",
                entity_type="external",
                loader=lambda: [
                    {"id": "read-only-1", "name": "Remote", "data": {"value": 1}},
                ],
                schema={"type": "object"},
            )
        )
    # Entity insertion legitimately advances Document.updated_at.  Pin the
    # final business head only after fixture construction is complete.
    document._created_at = 1
    document._updated_at = 20.5
    return document


def _capture(*, include_read_only: bool = True) -> CaptureResult:
    return capture_business_snapshot(
        "project-main", 12, _make_document(include_read_only=include_read_only)
    )


def test_runtime_mutation_policy_is_excluded_from_business_snapshot_root():
    document = _make_document(include_read_only=False)
    before = capture_business_snapshot("project-main", 12, document).snapshot
    before_version = document.version
    document._install_mutation_policy_authority({
        "card": {
            "schema": "ze.entity-mutation-policy.v1",
            "lifecycle": {
                "create": [],
                "rename": [],
                "soft_delete": [],
                "restore": [],
                "hard_remove": [],
            },
            "fields": {"title": ["Card.Update"], "score": ["Card.Update"]},
        },
    })
    after = capture_business_snapshot("project-main", 12, document).snapshot

    assert document.version == before_version
    assert after == before
    assert after.project.snapshot_sha256 == before.project.snapshot_sha256


def _entity_index(snapshot: BusinessSnapshotSet, entity_type: str, entity_id: str) -> int:
    for index, row in enumerate(snapshot.entities):
        if (row.entity_type, row.entity_id) == (entity_type, entity_id):
            return index
    raise AssertionError("entity row not found")


def _row_with_payload(row: Any, payload: bytes) -> Any:
    return replace(
        row,
        snapshot_bytes=payload,
        snapshot_sha256=payload_sha256(payload),
    )


def _canonical_entity_descriptors(
    rows: tuple[EntitySnapshotRow, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "codec_id": row.codec_id,
            "codec_version": row.codec_version,
            "deleted": bool(row.deleted),
            "entity_id": row.entity_id,
            "entity_type": row.entity_type,
            "entity_version": row.entity_version,
            "snapshot_format_version": row.snapshot_format_version,
            "snapshot_sha256": row.snapshot_sha256.hex(),
        }
        for row in rows
    ]


def _entity_set_sha256(rows: tuple[EntitySnapshotRow, ...]) -> str:
    hasher = hashlib.sha256()

    def frame(value: bytes) -> None:
        hasher.update(len(value).to_bytes(8, "big"))
        hasher.update(value)

    frame(ENTITY_SET_HASH_DOMAIN.encode("utf-8"))
    frame(len(rows).to_bytes(8, "big"))
    for descriptor in _canonical_entity_descriptors(rows):
        frame(encode_payload(descriptor))
    return hasher.hexdigest()


def _rebind_project(snapshot: BusinessSnapshotSet) -> BusinessSnapshotSet:
    """Rebuild only the aggregate descriptors after an adversarial row edit."""
    project_payload = decode_payload(snapshot.project.snapshot_bytes)
    project_payload["state"]["document"] = {
        "document_id": snapshot.document.document_id,
        "document_version": snapshot.document.document_version,
        "codec_id": snapshot.document.codec_id,
        "codec_version": snapshot.document.codec_version,
        "snapshot_format_version": snapshot.document.snapshot_format_version,
        "snapshot_sha256": snapshot.document.snapshot_sha256.hex(),
    }
    project_payload["state"]["entities"] = {
        "count": len(snapshot.entities),
        "set_sha256": _entity_set_sha256(snapshot.entities),
    }
    payload = encode_payload(project_payload)
    return replace(snapshot, project=_row_with_payload(snapshot.project, payload))


def _mutate_payload(
    snapshot: BusinessSnapshotSet,
    target: str,
    mutate: Callable[[dict[str, Any]], None],
    *,
    entity_index: int = 0,
    rebind: bool = True,
) -> BusinessSnapshotSet:
    if target == "project":
        row = snapshot.project
    elif target == "document":
        row = snapshot.document
    elif target == "entity":
        row = snapshot.entities[entity_index]
    else:
        raise AssertionError("unknown target")
    value = copy.deepcopy(decode_payload(row.snapshot_bytes))
    mutate(value)
    updated = _row_with_payload(row, encode_payload(value))
    if target == "project":
        changed = replace(snapshot, project=updated)
    elif target == "document":
        changed = replace(snapshot, document=updated)
    else:
        rows = list(snapshot.entities)
        rows[entity_index] = updated
        changed = replace(snapshot, entities=tuple(rows))
    if target != "project" and rebind:
        return _rebind_project(changed)
    return changed


def _json_object_at_container_depth(
    depth: int,
    leaf: Any = 0,
) -> dict[str, Any]:
    """Build an exact JSON object whose longest container path is ``depth``."""
    assert type(depth) is int and depth >= 1
    nested: Any = leaf
    for _ in range(depth - 1):
        nested = [nested]
    return {"value": nested}


def _replace_entity_row(
    snapshot: BusinessSnapshotSet,
    index: int,
    **changes: Any,
) -> BusinessSnapshotSet:
    rows = list(snapshot.entities)
    rows[index] = replace(rows[index], **changes)
    return _rebind_project(replace(snapshot, entities=tuple(rows)))


def test_snapshot_contract_constants_define_one_bounded_v1_format() -> None:
    assert snapshot_module.SNAPSHOT_FORMAT_VERSION == 1
    assert snapshot_module.PROJECT_SCHEMA == PROJECT_SCHEMA
    assert snapshot_module.DOCUMENT_SCHEMA == DOCUMENT_SCHEMA
    assert snapshot_module.ENTITY_SCHEMA == ENTITY_SCHEMA
    assert snapshot_module.ENTITY_SET_HASH_DOMAIN == ENTITY_SET_HASH_DOMAIN
    assert snapshot_module.MAX_PROJECT_SNAPSHOT_BYTES == 64 * 1024
    assert snapshot_module.MAX_DOCUMENT_SNAPSHOT_BYTES == 16 * 1024 * 1024
    assert snapshot_module.MAX_ENTITY_SNAPSHOT_BYTES == 8 * 1024 * 1024
    assert snapshot_module.MAX_ENTITY_COUNT == 1_000_000
    assert snapshot_module.MAX_TOTAL_SNAPSHOT_BYTES == 1024 * 1024 * 1024
    assert snapshot_module.MAX_SNAPSHOT_VALUE_NODES == 250_000
    assert snapshot_module.MAX_SQLITE_INTEGER == SQLITE_INT_MAX
    assert [field.name for field in dataclasses.fields(ProjectSnapshotRow)] == [
        "project_id",
        "generation",
        "codec_id",
        "codec_version",
        "snapshot_format_version",
        "snapshot_bytes",
        "snapshot_sha256",
    ]
    assert [field.name for field in dataclasses.fields(DocumentSnapshotRow)] == [
        "project_id",
        "document_id",
        "document_version",
        "schema_version",
        "collaboration_mode",
        "save_policy",
        "codec_id",
        "codec_version",
        "snapshot_format_version",
        "snapshot_bytes",
        "snapshot_sha256",
    ]
    assert [field.name for field in dataclasses.fields(EntitySnapshotRow)] == [
        "project_id",
        "document_id",
        "entity_type",
        "entity_id",
        "entity_version",
        "deleted",
        "codec_id",
        "codec_version",
        "snapshot_format_version",
        "snapshot_bytes",
        "snapshot_sha256",
    ]


def test_capture_emits_exact_envelopes_sql_rows_and_aggregate_digest() -> None:
    result = _capture()
    snapshot = result.snapshot

    assert result.excluded_read_only == (
        EntityKey(entity_type="external", entity_id="read-only-1"),
    )
    assert isinstance(snapshot, BusinessSnapshotSet)
    assert [(row.entity_type, row.entity_id) for row in snapshot.entities] == [
        ("asset", "middle-asset"),
        ("card", "a-card"),
        ("card", "z-card"),
    ]
    assert all(type(row.deleted) is int for row in snapshot.entities)
    assert [row.deleted for row in snapshot.entities] == [0, 1, 0]

    for row in (snapshot.project, snapshot.document, *snapshot.entities):
        assert row.codec_id == CODEC_ID
        assert row.codec_version == CODEC_VERSION == 1
        assert row.snapshot_format_version == 1
        assert type(row.snapshot_bytes) is bytes and row.snapshot_bytes
        assert type(row.snapshot_sha256) is bytes
        assert len(row.snapshot_sha256) == 32
        assert row.snapshot_sha256 == hashlib.sha256(row.snapshot_bytes).digest()

    document_payload = decode_payload(snapshot.document.snapshot_bytes)
    assert document_payload == {
        "schema": DOCUMENT_SCHEMA,
        "identity": {
            "project_id": "project-main",
            "document_id": "document/root",
        },
        "state": {
            "document_version": 8,
            "schema_version": "3.7.0",
            "collaboration_mode": "live_collab",
            "save_policy": "checkpoint_commit",
            "name": "主文档",
            "data_schema": _make_document()._schema,
            "config": {
                "viewport": {"width": 1920, "height": 1080},
                "flags": [True],
            },
            "acl": {
                "owner": "owner-1",
                "collaborators": ["user-2", "user-3"],
                "extension": {"reviewers": ["user-4"]},
            },
            "created_at": 1,
            "updated_at": 20.5,
        },
    }
    assert set(document_payload["state"]) == {
        "document_version",
        "schema_version",
        "collaboration_mode",
        "save_policy",
        "name",
        "data_schema",
        "config",
        "acl",
        "created_at",
        "updated_at",
    }
    assert "effective_save_policy" not in snapshot.document.snapshot_bytes.decode("utf-8")
    assert "harvest_rules" not in snapshot.document.snapshot_bytes.decode("utf-8")
    assert "read_only_sources" not in snapshot.document.snapshot_bytes.decode("utf-8")

    z_row = snapshot.entities[_entity_index(snapshot, "card", "z-card")]
    z_payload = decode_payload(z_row.snapshot_bytes)
    assert set(z_payload) == {"schema", "identity", "state"}
    assert z_payload["schema"] == ENTITY_SCHEMA
    assert z_payload["identity"] == {
        "project_id": "project-main",
        "document_id": "document/root",
        "entity_type": "card",
        "entity_id": "z-card",
    }
    assert set(z_payload["state"]) == {
        "entity_version",
        "collection_position",
        "deleted",
        "name",
        "data",
        "provenance",
        "actor_log",
        "created_at",
        "updated_at",
    }
    assert z_payload["state"]["entity_version"] == 4
    assert z_payload["state"]["collection_position"] == 0
    assert z_payload["state"]["deleted"] is False
    assert z_payload["state"]["actor_log"][1]["fields"] == ["z", "a"]
    assert type(z_payload["state"]["provenance"]["title"]["cost_usd"]) is int
    for forbidden in ("read_only", "schema", "validator"):
        assert forbidden not in z_payload["state"]

    project_payload = decode_payload(snapshot.project.snapshot_bytes)
    assert project_payload == {
        "schema": PROJECT_SCHEMA,
        "identity": {"project_id": "project-main"},
        "state": {
            "generation": 12,
            "document": {
                "document_id": snapshot.document.document_id,
                "document_version": snapshot.document.document_version,
                "codec_id": snapshot.document.codec_id,
                "codec_version": snapshot.document.codec_version,
                "snapshot_format_version": snapshot.document.snapshot_format_version,
                "snapshot_sha256": snapshot.document.snapshot_sha256.hex(),
            },
            "entities": {
                "count": 3,
                "set_sha256": _entity_set_sha256(snapshot.entities),
            },
        },
    }
    assert verify_business_snapshot(snapshot) is snapshot


@pytest.mark.parametrize(
    ("mode", "save_policy"),
    [
        ("live_collab", None),
        ("with_save", "legacy"),
        ("realtime_checkpoint", "auto_commit"),
        ("live_collab", "checkpoint_commit"),
    ],
)
def test_all_declared_collaboration_and_save_policy_states_roundtrip_exactly(
    mode: str, save_policy: Any
) -> None:
    document = Document(
        id="document-id",
        schema_version="1",
        collaboration_mode=mode,
        save_policy=save_policy,
    )
    document._created_at = 1
    document._updated_at = 1
    result = capture_business_snapshot("project-id", 1, document)
    payload = decode_payload(result.snapshot.document.snapshot_bytes)
    assert payload["state"]["collaboration_mode"] == mode
    assert payload["state"]["save_policy"] == save_policy
    restored = restore_business_snapshot(result.snapshot)
    assert restored.document.collaboration_mode == mode
    assert restored.document.save_policy == save_policy
    assert capture_business_snapshot("project-id", 1, restored.document).snapshot == result.snapshot


def test_capture_uses_python_unicode_tuple_order_without_locale_rules() -> None:
    document = Document(
        id="document-id",
        schema_version="1",
        collaboration_mode="live_collab",
    )
    for entity_type, entity_id in reversed(
        [("A", "中"), ("a", "z"), ("a", "中"), ("中", "a")]
    ):
        document.add(Entity(type=entity_type, id=entity_id, data={}))
    result = capture_business_snapshot("project-id", 1, document)
    keys = [(row.entity_type, row.entity_id) for row in result.snapshot.entities]
    assert keys == sorted(keys)


def test_snapshot_rows_are_frozen_passive_carriers_and_hide_payloads_from_repr() -> None:
    snapshot = _capture().snapshot
    for row in (snapshot.project, snapshot.document, *snapshot.entities):
        assert dataclasses.is_dataclass(row)
        rendered = repr(row)
        assert "snapshot_bytes" not in rendered
        assert "snapshot_sha256" not in rendered
        with pytest.raises(FrozenInstanceError):
            row.snapshot_bytes = b"changed"  # type: ignore[misc]

    # Constructors do not silently normalize hostile SQL values.  Verification
    # is the single fail-closed boundary.
    hostile = replace(snapshot.project, generation=True)
    assert hostile.generation is True
    _assert_snapshot_error(lambda: verify_business_snapshot(replace(snapshot, project=hostile)))


def test_capture_detaches_aliases_and_does_not_execute_deepcopy_hooks() -> None:
    shared = {"items": [1, 2]}
    document = _make_document(include_read_only=False)
    document._config = {"left": shared, "right": shared}
    entity = document.get("asset", "middle-asset")
    entity._data = {"left": shared, "right": shared}

    snapshot = capture_business_snapshot("project-main", 1, document).snapshot
    document_payload = decode_payload(snapshot.document.snapshot_bytes)
    entity_payload = decode_payload(
        snapshot.entities[_entity_index(snapshot, "asset", "middle-asset")].snapshot_bytes
    )
    assert document_payload["state"]["config"]["left"] is not document_payload["state"]["config"]["right"]
    assert entity_payload["state"]["data"]["left"] is not entity_payload["state"]["data"]["right"]
    shared["items"].append(99)
    assert decode_payload(snapshot.document.snapshot_bytes)["state"]["config"]["left"] == {
        "items": [1, 2]
    }

    called = []

    class HostileDict(dict):
        def __deepcopy__(self, memo):
            called.append(True)
            raise AssertionError("untrusted __deepcopy__ executed")

    document._config = {"hostile": HostileDict({"secret": "value"})}
    _assert_snapshot_error(lambda: capture_business_snapshot("project-main", 1, document))
    assert called == []

    document._config = {}
    hostile_provenance = AiProvenance(actor="u", model="m")
    object.__setattr__(
        hostile_provenance,
        "prompt_args",
        HostileDict({"secret": "value"}),
    )
    entity._provenance = {
        "sha": hostile_provenance,
    }
    _assert_snapshot_error(lambda: capture_business_snapshot("project-main", 1, document))
    assert called == []


@pytest.mark.parametrize("generation", [True, False, 0, -1, SQLITE_INT_MAX + 1, 1.0, "1"])
def test_capture_rejects_non_sqlite_generations(generation: Any) -> None:
    _assert_snapshot_error(
        lambda: capture_business_snapshot(
            "project-main", generation, _make_document(include_read_only=False)
        )
    )


def test_sqlite_integer_upper_bound_is_accepted_exactly() -> None:
    result = capture_business_snapshot(
        "project-main", SQLITE_INT_MAX, _make_document(include_read_only=False)
    )
    assert result.snapshot.project.generation == SQLITE_INT_MAX
    assert verify_business_snapshot(result.snapshot) == result.snapshot


@pytest.mark.parametrize(
    "project_id",
    [
        "",
        ".",
        "..",
        ".ze-private",
        "a/b",
        "a\\b",
        "a\x00b",
        "a\nb",
        "a\rb",
        "a\tb",
        "x" * 161,
        "\ud800",
    ],
)
def test_capture_rejects_unsafe_project_identity(project_id: str) -> None:
    _assert_snapshot_error(
        lambda: capture_business_snapshot(
            project_id, 1, _make_document(include_read_only=False)
        )
    )


def test_identity_requires_exact_strings_but_document_and_entity_ids_are_not_paths() -> None:
    class StringSubclass(str):
        pass

    document = _make_document(include_read_only=False)
    _assert_snapshot_error(
        lambda: capture_business_snapshot(StringSubclass("project-main"), 1, document)
    )

    document._id = StringSubclass("document")
    _assert_snapshot_error(lambda: capture_business_snapshot("project-main", 1, document))
    document._id = "document/with\\separators\n\x00"
    entity = document.get("asset", "middle-asset")
    entity._id = "entity/with\\separators\n\x00"
    # Keep bucket identity bound to the changed Entity identity.
    del document._collections["asset"]["middle-asset"]
    document._collections["asset"][entity.id] = entity
    snapshot = capture_business_snapshot("project-main", 1, document).snapshot
    assert snapshot.document.document_id == document.id
    assert any(row.entity_id == entity.id for row in snapshot.entities)
    assert verify_business_snapshot(snapshot) == snapshot

    document._id = "\ud800"
    _assert_snapshot_error(lambda: capture_business_snapshot("project-main", 1, document))


def test_capture_rejects_reserved_or_corrupt_collection_identity() -> None:
    document = _make_document(include_read_only=False)
    entity = document.get("asset", "middle-asset")
    document._collections["asset"]["wrong-key"] = entity
    _assert_snapshot_error(lambda: capture_business_snapshot("project-main", 1, document))

    document = _make_document(include_read_only=False)
    entity = document.get("asset", "middle-asset")
    del document._collections["asset"][entity.id]
    document._collections["wrong-type"][entity.id] = entity
    _assert_snapshot_error(lambda: capture_business_snapshot("project-main", 1, document))

    document = _make_document(include_read_only=False)
    document._collections["__document__"]["meta"] = Entity(
        type="__document__", id="meta", data={}
    )
    _assert_snapshot_error(lambda: capture_business_snapshot("project-main", 1, document))


def test_capture_includes_soft_delete_excludes_read_only_and_omits_hard_remove() -> None:
    document = _make_document()
    document.hard_remove("asset", "middle-asset")
    result = capture_business_snapshot("project-main", 2, document)
    keys = [(row.entity_type, row.entity_id) for row in result.snapshot.entities]
    assert ("asset", "middle-asset") not in keys
    assert ("card", "a-card") in keys
    deleted = result.snapshot.entities[_entity_index(result.snapshot, "card", "a-card")]
    assert deleted.deleted == 1
    assert decode_payload(deleted.snapshot_bytes)["state"]["deleted"] is True
    assert ("external", "read-only-1") not in keys
    assert result.excluded_read_only == (
        EntityKey(entity_type="external", entity_id="read-only-1"),
    )


def test_capture_never_reinvokes_read_only_loaders() -> None:
    calls = []

    def loader() -> list[dict[str, Any]]:
        calls.append("called")
        return [{"id": "remote", "data": {"value": 1}}]

    document = Document(
        id="document-id",
        schema_version="1",
        collaboration_mode="live_collab",
    )
    document.register_read_only_source(
        ReadOnlySourceSpec(name="remote", entity_type="external", loader=loader)
    )
    assert calls == ["called"]
    result = capture_business_snapshot("project-id", 1, document)
    assert calls == ["called"]
    assert result.excluded_read_only == (
        EntityKey(entity_type="external", entity_id="remote"),
    )


def test_verifier_requires_exact_snapshot_row_and_tuple_types() -> None:
    snapshot = _capture().snapshot
    _assert_snapshot_error(lambda: verify_business_snapshot(None))  # type: ignore[arg-type]
    _assert_snapshot_error(
        lambda: verify_business_snapshot(
            BusinessSnapshotSet(
                project=snapshot.project,
                document=snapshot.document,
                entities=list(snapshot.entities),  # type: ignore[arg-type]
            )
        )
    )

    class ProjectRowSubclass(ProjectSnapshotRow):
        pass

    class DocumentRowSubclass(DocumentSnapshotRow):
        pass

    class EntityRowSubclass(EntitySnapshotRow):
        pass

    project_subclass = ProjectRowSubclass(
        **{field.name: getattr(snapshot.project, field.name) for field in dataclasses.fields(snapshot.project)}
    )
    document_subclass = DocumentRowSubclass(
        **{field.name: getattr(snapshot.document, field.name) for field in dataclasses.fields(snapshot.document)}
    )
    first = snapshot.entities[0]
    entity_subclass = EntityRowSubclass(
        **{field.name: getattr(first, field.name) for field in dataclasses.fields(first)}
    )
    _assert_snapshot_error(
        lambda: verify_business_snapshot(replace(snapshot, project=project_subclass))
    )
    _assert_snapshot_error(
        lambda: verify_business_snapshot(replace(snapshot, document=document_subclass))
    )
    _assert_snapshot_error(
        lambda: verify_business_snapshot(
            replace(snapshot, entities=(entity_subclass,) + snapshot.entities[1:])
        )
    )


def test_verifier_rejects_bad_fixed_columns_bytes_hashes_and_integer_types() -> None:
    snapshot = _capture().snapshot
    class StringSubclass(str):
        pass

    project_cases = [
        replace(snapshot.project, project_id=1),
        replace(snapshot.project, project_id=StringSubclass("project-main")),
        replace(snapshot.project, generation=True),
        replace(snapshot.project, generation=0),
        replace(snapshot.project, generation=SQLITE_INT_MAX + 1),
        replace(snapshot.project, codec_id="other"),
        replace(snapshot.project, codec_version=2),
        replace(snapshot.project, snapshot_format_version=2),
        replace(snapshot.project, snapshot_bytes=bytearray(snapshot.project.snapshot_bytes)),
        replace(snapshot.project, snapshot_sha256=bytearray(snapshot.project.snapshot_sha256)),
        replace(snapshot.project, snapshot_sha256=b"x" * 31),
        replace(snapshot.project, snapshot_sha256=b"x" * 32),
    ]
    for row in project_cases:
        _assert_snapshot_error(lambda row=row: verify_business_snapshot(replace(snapshot, project=row)))

    document_cases = [
        replace(snapshot.document, project_id=1),
        replace(snapshot.document, project_id=StringSubclass("project-main")),
        replace(snapshot.document, document_id=1),
        replace(snapshot.document, document_id=StringSubclass("document/root")),
        replace(snapshot.document, document_version=True),
        replace(snapshot.document, document_version=0),
        replace(snapshot.document, document_version=SQLITE_INT_MAX + 1),
        replace(snapshot.document, schema_version=1),
        replace(snapshot.document, collaboration_mode="unknown"),
        replace(snapshot.document, save_policy="manual"),
        replace(snapshot.document, codec_id="other"),
        replace(snapshot.document, codec_version=0),
        replace(snapshot.document, snapshot_format_version=True),
        replace(snapshot.document, snapshot_bytes=memoryview(snapshot.document.snapshot_bytes)),
        replace(snapshot.document, snapshot_sha256=b"x" * 32),
    ]
    for row in document_cases:
        _assert_snapshot_error(lambda row=row: verify_business_snapshot(replace(snapshot, document=row)))

    index = _entity_index(snapshot, "card", "a-card")
    row = snapshot.entities[index]
    entity_cases = [
        replace(row, project_id=1),
        replace(row, document_id=1),
        replace(row, entity_type=1),
        replace(row, entity_type=StringSubclass(row.entity_type)),
        replace(row, entity_id=1),
        replace(row, entity_id=StringSubclass(row.entity_id)),
        replace(row, entity_version=True),
        replace(row, entity_version=0),
        replace(row, entity_version=SQLITE_INT_MAX + 1),
        replace(row, deleted=True),
        replace(row, deleted=-1),
        replace(row, deleted=2),
        replace(row, codec_id="other"),
        replace(row, codec_version=2),
        replace(row, snapshot_format_version=0),
        replace(row, snapshot_bytes=bytearray(row.snapshot_bytes)),
        replace(row, snapshot_sha256=b"x" * 32),
    ]
    for changed in entity_cases:
        rows = list(snapshot.entities)
        rows[index] = changed
        _assert_snapshot_error(
            lambda rows=rows: verify_business_snapshot(replace(snapshot, entities=tuple(rows)))
        )


class _HostileRowScalar:
    """Invalid row value whose ordinary scalar operations execute code."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def _trap(self, operation: str) -> Any:
        self._calls.append(operation)
        raise AssertionError("hostile row scalar hook executed: " + operation)

    def __eq__(self, other: Any) -> Any:
        return self._trap("__eq__")

    def __ne__(self, other: Any) -> Any:
        return self._trap("__ne__")

    def __lt__(self, other: Any) -> Any:
        return self._trap("__lt__")

    def __le__(self, other: Any) -> Any:
        return self._trap("__le__")

    def __gt__(self, other: Any) -> Any:
        return self._trap("__gt__")

    def __ge__(self, other: Any) -> Any:
        return self._trap("__ge__")

    def __len__(self) -> int:
        return self._trap("__len__")

    def __bool__(self) -> bool:
        return self._trap("__bool__")

    def __iter__(self) -> Any:
        return self._trap("__iter__")

    def __contains__(self, item: Any) -> bool:
        return self._trap("__contains__")

    def __index__(self) -> int:
        return self._trap("__index__")

    def encode(self, *args: Any, **kwargs: Any) -> bytes:
        return self._trap("encode")

    def startswith(self, *args: Any, **kwargs: Any) -> bool:
        return self._trap("startswith")

    def hex(self) -> str:
        return self._trap("hex")


def _replace_row_scalar(
    snapshot: BusinessSnapshotSet,
    target: str,
    field_name: str,
    value: Any,
) -> BusinessSnapshotSet:
    if target == "project":
        return replace(
            snapshot,
            project=replace(snapshot.project, **{field_name: value}),
        )
    if target == "document":
        return replace(
            snapshot,
            document=replace(snapshot.document, **{field_name: value}),
        )
    if target == "entity":
        rows = list(snapshot.entities)
        rows[0] = replace(rows[0], **{field_name: value})
        return replace(snapshot, entities=tuple(rows))
    raise AssertionError("unknown row target")


@pytest.mark.parametrize("target", ["project", "document", "entity"])
def test_hostile_codec_id_comparison_hooks_are_never_executed(target: str) -> None:
    snapshot = _capture().snapshot
    calls: list[str] = []
    changed = _replace_row_scalar(
        snapshot,
        target,
        "codec_id",
        _HostileRowScalar(calls),
    )

    _assert_snapshot_error(lambda: verify_business_snapshot(changed))
    assert calls == []


@pytest.mark.parametrize(
    ("target", "field_name"),
    [
        ("project", "project_id"),
        ("project", "generation"),
        ("project", "codec_version"),
        ("project", "snapshot_format_version"),
        ("project", "snapshot_bytes"),
        ("project", "snapshot_sha256"),
        ("document", "project_id"),
        ("document", "document_id"),
        ("document", "document_version"),
        ("document", "schema_version"),
        ("document", "collaboration_mode"),
        ("document", "save_policy"),
        ("document", "codec_version"),
        ("document", "snapshot_format_version"),
        ("document", "snapshot_bytes"),
        ("document", "snapshot_sha256"),
        ("entity", "project_id"),
        ("entity", "document_id"),
        ("entity", "entity_type"),
        ("entity", "entity_id"),
        ("entity", "entity_version"),
        ("entity", "deleted"),
        ("entity", "codec_version"),
        ("entity", "snapshot_format_version"),
        ("entity", "snapshot_bytes"),
        ("entity", "snapshot_sha256"),
    ],
)
def test_every_other_row_scalar_checks_exact_type_before_value_operations(
    target: str,
    field_name: str,
) -> None:
    snapshot = _capture().snapshot
    calls: list[str] = []
    changed = _replace_row_scalar(
        snapshot,
        target,
        field_name,
        _HostileRowScalar(calls),
    )

    _assert_snapshot_error(lambda: verify_business_snapshot(changed))
    assert calls == []


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [("project_id", 1), ("generation", True)],
)
def test_project_hash_precedes_passive_sql_scalar_semantics(
    field_name: str,
    invalid_value: Any,
) -> None:
    snapshot = _capture().snapshot
    changed = replace(
        snapshot,
        project=replace(
            snapshot.project,
            **{field_name: invalid_value, "snapshot_sha256": b"x" * 32},
        ),
    )
    error = _assert_snapshot_error(lambda: verify_business_snapshot(changed))
    assert error.code == "business_snapshot_hash_invalid"
    assert error.path == "$.project.snapshot_sha256"


def test_verifier_rejects_noncanonical_payload_even_when_hash_and_root_match() -> None:
    snapshot = _capture().snapshot
    payload = b" " + snapshot.document.snapshot_bytes
    changed = replace(snapshot, document=_row_with_payload(snapshot.document, payload))
    changed = _rebind_project(changed)
    _assert_snapshot_error(lambda: verify_business_snapshot(changed))


def test_invalid_canonical_row_bytes_do_not_retain_codec_exception_or_payload() -> None:
    snapshot = _capture().snapshot
    marker = "canonical-row-secret-marker"
    invalid_canonical_bytes = (
        '{"secret":"%s", "value":1}' % marker
    ).encode("utf-8")
    changed = replace(
        snapshot,
        document=_row_with_payload(snapshot.document, invalid_canonical_bytes),
    )
    changed = _rebind_project(changed)

    error = _assert_snapshot_error(lambda: verify_business_snapshot(changed))
    _assert_clean_exception_boundary(error, marker)


def test_invalid_live_codec_value_does_not_retain_codec_exception_or_value() -> None:
    marker = "live-codec-secret-marker"

    class UnsupportedLiveValue:
        def __repr__(self) -> str:
            return marker

    document = _make_document(include_read_only=False)
    document._config = {"private": UnsupportedLiveValue()}

    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    _assert_clean_exception_boundary(error, marker)


def test_entity_set_codec_failure_does_not_retain_lower_exception_payload(
    monkeypatch,
) -> None:
    marker = "entity-set-secret-marker"
    real_encode_payload = snapshot_module.encode_payload_bounded

    def fail_entity_set(value: Any, maximum_bytes: int, maximum_nodes: int) -> bytes:
        if type(value) is dict and set(value) == {
            "codec_id",
            "codec_version",
            "deleted",
            "entity_id",
            "entity_type",
            "entity_version",
            "snapshot_format_version",
            "snapshot_sha256",
        }:
            failure = BusinessCodecError(
                "business_codec_type_unsupported",
                "$",
                "unsupported",
            )
            failure.retained_payload = {"secret": marker}
            raise failure
        return real_encode_payload(value, maximum_bytes, maximum_nodes)

    monkeypatch.setattr(snapshot_module, "encode_payload_bounded", fail_entity_set)
    document = _make_document(include_read_only=False)

    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    _assert_clean_exception_boundary(error, marker)


@pytest.mark.parametrize("target", ["project", "document", "entity"])
def test_every_payload_envelope_requires_exact_outer_identity_and_state_keys(target: str) -> None:
    snapshot = _capture().snapshot
    entity_index = _entity_index(snapshot, "card", "z-card")

    def extra_outer(value: dict[str, Any]) -> None:
        value["extra"] = 1

    def missing_schema(value: dict[str, Any]) -> None:
        del value["schema"]

    def extra_identity(value: dict[str, Any]) -> None:
        value["identity"]["extra"] = 1

    def missing_identity(value: dict[str, Any]) -> None:
        del value["identity"]

    def extra_state(value: dict[str, Any]) -> None:
        value["state"]["extra"] = 1

    def missing_state(value: dict[str, Any]) -> None:
        del value["state"]

    def wrong_schema(value: dict[str, Any]) -> None:
        value["schema"] = "ze.business-snapshot.wrong.v1"

    for mutation in (
        extra_outer,
        missing_schema,
        extra_identity,
        missing_identity,
        extra_state,
        missing_state,
        wrong_schema,
    ):
        changed = _mutate_payload(
            snapshot,
            target,
            mutation,
            entity_index=entity_index,
        )
        _assert_snapshot_error(lambda changed=changed: verify_business_snapshot(changed))


def test_payload_identity_and_sql_columns_are_bidirectionally_bound() -> None:
    snapshot = _capture().snapshot
    index = _entity_index(snapshot, "card", "z-card")

    changed = replace(snapshot, project=replace(snapshot.project, generation=13))
    _assert_snapshot_error(lambda: verify_business_snapshot(changed))

    changed = replace(
        snapshot,
        document=replace(snapshot.document, document_version=9),
    )
    changed = _rebind_project(changed)
    _assert_snapshot_error(lambda: verify_business_snapshot(changed))

    changed = _replace_entity_row(snapshot, index, entity_version=3)
    _assert_snapshot_error(lambda: verify_business_snapshot(changed))
    changed = _replace_entity_row(snapshot, index, deleted=1)
    _assert_snapshot_error(lambda: verify_business_snapshot(changed))

    def wrong_document_identity(value: dict[str, Any]) -> None:
        value["identity"]["document_id"] = "another-document"

    changed = _mutate_payload(snapshot, "document", wrong_document_identity)
    _assert_snapshot_error(lambda: verify_business_snapshot(changed))

    def wrong_entity_identity(value: dict[str, Any]) -> None:
        value["identity"]["entity_id"] = "another-entity"

    changed = _mutate_payload(
        snapshot, "entity", wrong_entity_identity, entity_index=index
    )
    _assert_snapshot_error(lambda: verify_business_snapshot(changed))


def test_entity_rows_must_be_preordered_unique_and_share_one_topology() -> None:
    snapshot = _capture().snapshot
    assert len(snapshot.entities) >= 2

    # Leaving the original root intact makes this an important regression: a
    # verifier that silently sorts untrusted rows would accept it.
    reordered = replace(snapshot, entities=tuple(reversed(snapshot.entities)))
    _assert_snapshot_error(lambda: verify_business_snapshot(reordered))

    duplicated = replace(snapshot, entities=snapshot.entities + (snapshot.entities[0],))
    duplicated = _rebind_project(duplicated)
    _assert_snapshot_error(lambda: verify_business_snapshot(duplicated))

    wrong_project = _replace_entity_row(
        snapshot, 0, project_id="another-project"
    )
    _assert_snapshot_error(lambda: verify_business_snapshot(wrong_project))
    wrong_document = _replace_entity_row(
        snapshot, 0, document_id="another-document"
    )
    _assert_snapshot_error(lambda: verify_business_snapshot(wrong_document))
    reserved = _replace_entity_row(snapshot, 0, entity_type="__document__")
    _assert_snapshot_error(lambda: verify_business_snapshot(reserved))


def test_project_root_rejects_document_or_entity_set_descriptor_tampering() -> None:
    snapshot = _capture().snapshot

    mutations = []

    def wrong_document_hash(value: dict[str, Any]) -> None:
        value["state"]["document"]["snapshot_sha256"] = "0" * 64

    mutations.append(wrong_document_hash)

    def wrong_document_version(value: dict[str, Any]) -> None:
        value["state"]["document"]["document_version"] += 1

    mutations.append(wrong_document_version)

    def wrong_count(value: dict[str, Any]) -> None:
        value["state"]["entities"]["count"] += 1

    mutations.append(wrong_count)

    def bool_count(value: dict[str, Any]) -> None:
        value["state"]["entities"]["count"] = True

    mutations.append(bool_count)

    def wrong_set_hash(value: dict[str, Any]) -> None:
        value["state"]["entities"]["set_sha256"] = "0" * 64

    mutations.append(wrong_set_hash)

    def uppercase_set_hash(value: dict[str, Any]) -> None:
        value["state"]["entities"]["set_sha256"] = "A" * 64

    mutations.append(uppercase_set_hash)

    def extra_document_member(value: dict[str, Any]) -> None:
        value["state"]["document"]["extra"] = 1

    mutations.append(extra_document_member)

    def missing_document_member(value: dict[str, Any]) -> None:
        del value["state"]["document"]["codec_version"]

    mutations.append(missing_document_member)

    def extra_entity_set_member(value: dict[str, Any]) -> None:
        value["state"]["entities"]["extra"] = 1

    mutations.append(extra_entity_set_member)

    def missing_entity_set_member(value: dict[str, Any]) -> None:
        del value["state"]["entities"]["count"]

    mutations.append(missing_entity_set_member)

    for mutation in mutations:
        changed = _mutate_payload(snapshot, "project", mutation)
        _assert_snapshot_error(lambda changed=changed: verify_business_snapshot(changed))


def test_document_state_types_schema_acl_and_timestamps_fail_closed() -> None:
    snapshot = _capture().snapshot

    def set_state(member: str, value: Any) -> Callable[[dict[str, Any]], None]:
        def mutate(payload: dict[str, Any]) -> None:
            payload["state"][member] = value

        return mutate

    invalid = [
        set_state("document_version", True),
        set_state("schema_version", 1),
        set_state("collaboration_mode", "unknown"),
        set_state("save_policy", "manual"),
        set_state("save_policy", 0),
        set_state("name", 1),
        set_state("data_schema", []),
        set_state("data_schema", {"": {}}),
        set_state("data_schema", {"card": []}),
        set_state("config", []),
        set_state("acl", []),
        set_state("acl", {"owner": 1}),
        set_state("acl", {"collaborators": "u"}),
        set_state("acl", {"collaborators": ["u", "u"]}),
        set_state("created_at", True),
        set_state("created_at", -1),
        set_state("updated_at", True),
        set_state("updated_at", -1),
    ]
    for mutation in invalid:
        changed = _mutate_payload(snapshot, "document", mutation)
        _assert_snapshot_error(lambda changed=changed: verify_business_snapshot(changed))

    def reverse_time(payload: dict[str, Any]) -> None:
        payload["state"]["created_at"] = 21
        payload["state"]["updated_at"] = 20

    changed = _mutate_payload(snapshot, "document", reverse_time)
    _assert_snapshot_error(lambda: verify_business_snapshot(changed))

    # ACL extension keys are forward-compatible, and both server-owned members
    # are optional.  Verification must not invent normalized keys.
    def extension_only(payload: dict[str, Any]) -> None:
        payload["state"]["acl"] = {"future_rule": {"enabled": True}}

    changed = _mutate_payload(snapshot, "document", extension_only)
    assert verify_business_snapshot(changed) == changed


def test_entity_state_and_timestamp_types_fail_closed() -> None:
    snapshot = _capture().snapshot
    index = _entity_index(snapshot, "card", "z-card")

    def set_state(member: str, value: Any) -> Callable[[dict[str, Any]], None]:
        def mutate(payload: dict[str, Any]) -> None:
            payload["state"][member] = value

        return mutate

    invalid = [
        set_state("entity_version", True),
        set_state("entity_version", 0),
        set_state("deleted", 0),
        set_state("name", 1),
        set_state("data", []),
        set_state("provenance", []),
        set_state("actor_log", {}),
        set_state("created_at", True),
        set_state("created_at", -1),
        set_state("updated_at", True),
        set_state("updated_at", -1),
    ]
    for mutation in invalid:
        changed = _mutate_payload(
            snapshot, "entity", mutation, entity_index=index
        )
        _assert_snapshot_error(lambda changed=changed: verify_business_snapshot(changed))

    def reverse_time(payload: dict[str, Any]) -> None:
        payload["state"]["created_at"] = 10
        payload["state"]["updated_at"] = 9

    changed = _mutate_payload(snapshot, "entity", reverse_time, entity_index=index)
    _assert_snapshot_error(lambda: verify_business_snapshot(changed))


@pytest.mark.parametrize(
    ("target", "member", "expected_path"),
    [
        ("document", "data_schema", "$.document.state.data_schema"),
        ("document", "config", "$.document.state.config"),
        ("document", "acl", "$.document.state.acl"),
        ("entity", "data", "$.entities[2].state.data"),
        (
            "entity",
            "prompt_args",
            "$.entities[2].state.provenance.prompt_args",
        ),
    ],
)
def test_verify_rejects_codec_valid_values_outside_the_120_level_domain(
    target: str,
    member: str,
    expected_path: str,
) -> None:
    """A fully re-rooted row may be valid codec v1 but impossible to restore."""
    snapshot = _capture().snapshot
    entity_index = _entity_index(snapshot, "card", "z-card")
    assert entity_index == 2
    secret = "TOPSECRET-domain-depth"
    too_deep = _json_object_at_container_depth(121, secret)

    def mutate(payload: dict[str, Any]) -> None:
        if member == "prompt_args":
            payload["state"]["provenance"]["title"][member] = too_deep
        elif member == "data_schema":
            # The schema map adds one owner container around the independently
            # valid 120-level type schema, producing a 121-level Document value.
            payload["state"][member] = {
                "card": _json_object_at_container_depth(120, secret),
            }
        else:
            payload["state"][member] = too_deep

    changed = _mutate_payload(
        snapshot,
        target,
        mutate,
        entity_index=entity_index,
    )
    error = _assert_snapshot_error(lambda: verify_business_snapshot(changed))
    assert error.code == (
        "business_snapshot_schema_invalid"
        if member == "data_schema"
        else "business_snapshot_domain_value_invalid"
    )
    assert error.path == expected_path
    _assert_clean_exception_boundary(error, secret)


def test_120_level_business_values_capture_verify_restore_and_recapture() -> None:
    at_limit = _json_object_at_container_depth(120)
    document = Document(
        id="domain-depth-boundary",
        schema_version="1",
        collaboration_mode="live_collab",
        config=at_limit,
    )
    entity = Entity(type="item", id="deep", data=at_limit)
    entity._provenance = {
        "value": AiProvenance(
            actor="u",
            model="m",
            prompt_args=at_limit,
        ),
    }
    document.add(entity)

    snapshot = capture_business_snapshot("project-main", 1, document).snapshot
    assert verify_business_snapshot(snapshot) is snapshot
    restored = restore_business_snapshot(snapshot)
    assert restored.document.config == at_limit
    restored_entity = restored.document.get("item", "deep")
    assert restored_entity.data == at_limit
    assert restored_entity.provenance("value").prompt_args == at_limit
    assert capture_business_snapshot(
        restored.project_id,
        restored.generation,
        restored.document,
    ).snapshot == snapshot


def test_provenance_requires_the_exact_ai_provenance_contract() -> None:
    snapshot = _capture().snapshot
    index = _entity_index(snapshot, "card", "z-card")

    def mutate_provenance(
        mutation: Callable[[dict[str, Any]], None]
    ) -> BusinessSnapshotSet:
        def apply(payload: dict[str, Any]) -> None:
            mutation(payload["state"]["provenance"]["title"])

        return _mutate_payload(snapshot, "entity", apply, entity_index=index)

    def delete_key(key: str) -> Callable[[dict[str, Any]], None]:
        return lambda value: value.pop(key)

    def set_key(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
        def mutate(provenance: dict[str, Any]) -> None:
            provenance[key] = value

        return mutate

    mutations = [
        delete_key("trace_id"),
        set_key("extra", 1),
        set_key("actor", 1),
        set_key("model", 1),
        set_key("model_version", 1),
        set_key("prompt_template_id", 1),
        set_key("prompt_args", []),
        set_key("cost_usd", True),
        set_key("cost_usd", -1),
        set_key("token_in", True),
        set_key("token_in", 1.0),
        set_key("token_in", -1),
        set_key("token_out", True),
        set_key("token_out", -1),
        set_key("ts", True),
        set_key("ts", -1),
        set_key("trace_id", 1),
    ]
    for mutation in mutations:
        changed = mutate_provenance(mutation)
        _assert_snapshot_error(lambda changed=changed: verify_business_snapshot(changed))


def test_provenance_token_counts_use_codec_integer_domain_not_sqlite_column_domain() -> None:
    document = _make_document(include_read_only=False)
    entity = document.get("card", "z-card")
    provenance = entity._provenance["title"]
    token_above_sqlite_column_range = SQLITE_INT_MAX + 1
    entity._provenance["title"] = replace(
        provenance,
        token_in=token_above_sqlite_column_range,
    )

    captured = capture_business_snapshot("project-main", 12, document)
    snapshot = captured.snapshot
    index = _entity_index(snapshot, "card", "z-card")
    payload = decode_payload(snapshot.entities[index].snapshot_bytes)
    assert payload["state"]["provenance"]["title"]["token_in"] == (
        token_above_sqlite_column_range
    )
    assert type(payload["state"]["provenance"]["title"]["token_in"]) is int
    assert verify_business_snapshot(snapshot) is snapshot

    restored = restore_business_snapshot(snapshot)
    restored_provenance = restored.document.get("card", "z-card").provenance("title")
    assert restored_provenance is not None
    assert restored_provenance.token_in == token_above_sqlite_column_range
    assert type(restored_provenance.token_in) is int
    assert capture_business_snapshot(
        restored.project_id,
        restored.generation,
        restored.document,
    ).snapshot == snapshot


def test_actor_log_requires_exact_events_monotonic_versions_and_preserves_fields_order() -> None:
    snapshot = _capture().snapshot
    index = _entity_index(snapshot, "card", "z-card")

    def mutate_log(mutation: Callable[[list[dict[str, Any]]], None]) -> BusinessSnapshotSet:
        def apply(payload: dict[str, Any]) -> None:
            mutation(payload["state"]["actor_log"])

        return _mutate_payload(snapshot, "entity", apply, entity_index=index)

    def event_set(member: str, value: Any) -> Callable[[list[dict[str, Any]]], None]:
        def mutate(log: list[dict[str, Any]]) -> None:
            log[0][member] = value

        return mutate

    def actor_set(member: str, value: Any) -> Callable[[list[dict[str, Any]]], None]:
        def mutate(log: list[dict[str, Any]]) -> None:
            log[0]["actor"][member] = value

        return mutate

    mutations = [
        lambda log: log[0].pop("fields"),
        lambda log: log[0].update({"extra": 1}),
        event_set("version", True),
        event_set("version", 0),
        event_set("version", 5),
        event_set("ts", True),
        event_set("ts", -1),
        event_set("actor", []),
        event_set("fields", "title"),
        event_set("fields", ["title", 1]),
        actor_set("user_id", 1),
        actor_set("device_id", 1),
        actor_set("source", "unknown"),
        lambda log: log[0]["actor"].update({"extra": 1}),
        lambda log: log[0]["actor"].pop("device_id"),
        # Equal and decreasing versions are both forbidden; gaps are allowed.
        lambda log: log[1].update({"version": log[0]["version"]}),
        lambda log: log[1].update({"version": 1}),
    ]
    for mutation in mutations:
        changed = mutate_log(mutation)
        _assert_snapshot_error(lambda changed=changed: verify_business_snapshot(changed))

    assert verify_business_snapshot(snapshot) == snapshot
    payload = decode_payload(snapshot.entities[index].snapshot_bytes)
    assert [event["version"] for event in payload["state"]["actor_log"]] == [2, 4]
    assert payload["state"]["actor_log"][1]["fields"] == ["z", "a"]


def test_payload_size_ceilings_reject_oversized_rows_before_they_can_be_data() -> None:
    snapshot = _capture().snapshot
    index = _entity_index(snapshot, "card", "z-card")

    def huge_entity(payload: dict[str, Any]) -> None:
        payload["state"]["data"] = {"blob": "x" * (8 * 1024 * 1024)}

    changed = _mutate_payload(snapshot, "entity", huge_entity, entity_index=index)
    assert len(changed.entities[index].snapshot_bytes) > 8 * 1024 * 1024
    _assert_snapshot_error(lambda: verify_business_snapshot(changed))

    def huge_document(payload: dict[str, Any]) -> None:
        payload["state"]["config"] = {"blob": "x" * (16 * 1024 * 1024)}

    changed = _mutate_payload(snapshot, "document", huge_document)
    assert len(changed.document.snapshot_bytes) > 16 * 1024 * 1024
    _assert_snapshot_error(lambda: verify_business_snapshot(changed))

    oversized_project = encode_payload(
        {
            "schema": PROJECT_SCHEMA,
            "identity": {"project_id": "project-main"},
            "state": {
                "generation": 12,
                "document": {"padding": "x" * (64 * 1024)},
                "entities": {"count": 0, "set_sha256": "0" * 64},
            },
        }
    )
    assert len(oversized_project) > 64 * 1024
    changed = replace(
        snapshot,
        project=_row_with_payload(snapshot.project, oversized_project),
    )
    _assert_snapshot_error(lambda: verify_business_snapshot(changed))


def test_entity_count_and_total_byte_ceilings_are_enforced_on_capture_and_verify(
    monkeypatch,
) -> None:
    document = _make_document(include_read_only=False)
    snapshot = capture_business_snapshot("project-main", 1, document).snapshot

    original_count_limit = snapshot_module.MAX_ENTITY_COUNT
    monkeypatch.setattr(snapshot_module, "MAX_ENTITY_COUNT", 2)
    _assert_snapshot_error(lambda: verify_business_snapshot(snapshot))
    _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    monkeypatch.setattr(snapshot_module, "MAX_ENTITY_COUNT", original_count_limit)

    total = (
        len(snapshot.project.snapshot_bytes)
        + len(snapshot.document.snapshot_bytes)
        + sum(len(row.snapshot_bytes) for row in snapshot.entities)
    )
    monkeypatch.setattr(snapshot_module, "MAX_TOTAL_SNAPSHOT_BYTES", total - 1)
    _assert_snapshot_error(lambda: verify_business_snapshot(snapshot))
    _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )


def test_restore_roundtrip_is_byte_exact_detached_and_reinstalls_future_validation() -> None:
    original_document = _make_document()
    captured = capture_business_snapshot("project-main", 12, original_document)
    restored = restore_business_snapshot(captured.snapshot)

    assert isinstance(restored, RestoredBusinessHead)
    assert restored.project_id == "project-main"
    assert restored.generation == 12
    assert restored.requires_runtime_rehydration is True
    assert isinstance(restored.document, Document)
    assert restored.document is not original_document
    assert restored.document.id == "document/root"
    assert restored.document.version == 8
    assert restored.document.schema == original_document.schema
    assert restored.document.config == original_document.config
    assert restored.document.acl == original_document.acl
    assert restored.document.read_only_sources() == []
    assert restored.document.find("external", "read-only-1") is None

    original_z = original_document.get("card", "z-card")
    restored_z = restored.document.get("card", "z-card")
    assert restored_z is not original_z
    assert restored_z.version == 4
    assert restored_z.deleted is False
    assert restored_z.actor_log() == original_z.actor_log()
    assert restored_z.actor_log()[1]["fields"] == ["z", "a"]
    assert isinstance(restored_z.provenance("title"), AiProvenance)
    assert restored_z.provenance("title") == original_z.provenance("title")
    assert type(restored_z.provenance("title").cost_usd) is int
    assert restored_z.has_validator is True
    assert restored_z._document_owner is restored.document
    assert restored.document.get("card", "a-card").deleted is True

    recaptured = capture_business_snapshot(
        restored.project_id, restored.generation, restored.document
    )
    assert recaptured.excluded_read_only == ()
    assert recaptured.snapshot == captured.snapshot

    with pytest.raises(ZeError) as raised:
        restored_z.update(
            {"title": 123},
            expected_version=restored_z.version,
            actor=Actor(user_id="u"),
        )
    assert raised.value.code == "schema_violation"
    restored_z.update(
        {"title": "valid future write"},
        expected_version=restored_z.version,
        actor=Actor(user_id="u"),
    )
    assert restored_z.data["title"] == "valid future write"


def test_restored_business_head_has_no_public_constructor_and_is_frozen() -> None:
    document = _make_document(include_read_only=False)
    with pytest.raises(TypeError):
        RestoredBusinessHead(
            project_id="project-main", generation=1, document=document,
        )

    restored = restore_business_snapshot(
        capture_business_snapshot("project-main", 1, document).snapshot
    )
    with pytest.raises((AttributeError, TypeError)):
        restored.project_id = "forged"


def test_restore_attaches_validator_without_revalidating_historical_data() -> None:
    document = Document(
        id="historical-document",
        schema_version="1",
        collaboration_mode="with_save",
        schema={
            "card": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
            }
        },
    )
    historical = Entity(type="card", id="historical", data={"title": 123})
    # This models an already-durable row predating today's schema.  Capture is a
    # value boundary, not a surprise schema-migration job.  Attach the current
    # authoritative schema without revalidating the historical dirty value.
    document._add_entity_internal(historical)
    historical._created_at = 1
    historical._updated_at = 1
    document._created_at = 1
    document._updated_at = 1

    snapshot = capture_business_snapshot("project-main", 1, document).snapshot
    restored = restore_business_snapshot(snapshot)
    entity = restored.document.get("card", "historical")
    assert entity.data == {"title": 123}
    assert entity.has_validator is True
    assert capture_business_snapshot("project-main", 1, restored.document).snapshot == snapshot

    with pytest.raises(ZeError) as raised:
        entity.update(
            {"title": 456},
            expected_version=entity.version,
            actor=Actor(user_id="u"),
        )
    assert raised.value.code == "schema_violation"


def test_restore_verifies_everything_before_constructing_a_document(monkeypatch) -> None:
    snapshot = _capture().snapshot
    invalid = replace(
        snapshot,
        project=replace(snapshot.project, snapshot_sha256=b"x" * 32),
    )
    constructed = []

    def forbidden_constructor(*args: Any, **kwargs: Any) -> None:
        constructed.append(True)
        raise AssertionError("Document constructed before full verification")

    monkeypatch.setattr(snapshot_module, "Document", forbidden_constructor)
    _assert_snapshot_error(lambda: snapshot_module.restore_business_snapshot(invalid))
    assert constructed == []


def test_restore_constructor_failure_does_not_retain_lower_exception_payload(
    monkeypatch,
) -> None:
    snapshot = _capture(include_read_only=False).snapshot
    marker = "restore-constructor-secret-marker"

    def fail_document_construction(*args: Any, **kwargs: Any) -> None:
        failure = RuntimeError("domain constructor failed")
        failure.retained_payload = {"secret": marker}
        raise failure

    monkeypatch.setattr(snapshot_module, "Document", fail_document_construction)
    error = _assert_snapshot_error(
        lambda: snapshot_module.restore_business_snapshot(snapshot)
    )
    _assert_clean_exception_boundary(error, marker)


def test_capture_verify_restore_have_no_db_file_or_legacy_loader_side_effects(
    monkeypatch, tmp_path: Path
) -> None:
    document = _make_document(include_read_only=False)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("I/O or permissive legacy entry point was used")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(Document, "save", forbidden)
    monkeypatch.setattr(Document, "open", classmethod(forbidden))
    monkeypatch.setattr(Document, "add", forbidden)
    monkeypatch.setattr(Entity, "from_dict", classmethod(forbidden))

    captured = capture_business_snapshot("project-main", 1, document)
    assert verify_business_snapshot(captured.snapshot) == captured.snapshot
    restored = restore_business_snapshot(captured.snapshot)
    assert restored.document.id == document.id
    assert list(tmp_path.iterdir()) == []


def test_module_exposes_only_aggregate_snapshot_actions_not_wide_or_io_entrances() -> None:
    for forbidden in (
        "capture_document",
        "capture_entity",
        "restore_document",
        "restore_entity",
        "serialize_document",
        "write_snapshot",
        "read_snapshot",
        "import_legacy",
        "open_database",
        "connect",
    ):
        assert not hasattr(snapshot_module, forbidden)
    assert not hasattr(snapshot_module, "sqlite3")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document, entity: document.__dict__.__setitem__("_future", 1),
        lambda document, entity: document.__dict__.pop("_config"),
        lambda document, entity: entity.__dict__.__setitem__("_future", 1),
        lambda document, entity: entity.__dict__.pop("_validator"),
        lambda document, entity: entity.__dict__.pop("_document_owner"),
        lambda document, entity: entity.__dict__.pop("_document_admission_state"),
    ],
)
def test_capture_requires_exact_document_and_entity_state_inventory(mutation) -> None:
    document = _make_document(include_read_only=False)
    entity = document.get("card", "z-card")
    mutation(document, entity)
    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    assert error.code == "business_snapshot_domain_state_invalid"


@pytest.mark.parametrize("member", ["extra", "missing"])
def test_capture_requires_exact_ai_provenance_state_inventory(member: str) -> None:
    document = _make_document(include_read_only=False)
    provenance = document.get("card", "z-card")._provenance["title"]
    if member == "extra":
        object.__setattr__(provenance, "future_field", "future")
    else:
        object.__delattr__(provenance, "trace_id")
    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    assert error.code == "business_snapshot_domain_state_invalid"


def test_empty_provenance_field_key_remains_verify_restore_closed() -> None:
    document = _make_document(include_read_only=False)
    entity = document.get("asset", "middle-asset")
    entity._provenance = {"": AiProvenance(actor="u", model="m")}
    snapshot = capture_business_snapshot("project-main", 1, document).snapshot
    assert verify_business_snapshot(snapshot) is snapshot
    restored = restore_business_snapshot(snapshot)
    assert restored.document.get("asset", "middle-asset").provenance("") is not None
    assert (
        capture_business_snapshot("project-main", 1, restored.document).snapshot
        == snapshot
    )


@pytest.mark.parametrize(
    "replacement",
    [
        lambda collections: dict(collections),
        lambda collections: defaultdict(list, collections),
        lambda collections: defaultdict(lambda: {}, collections),
    ],
)
def test_capture_requires_exact_document_collection_container_semantics(
    replacement,
) -> None:
    document = _make_document(include_read_only=False)
    document._collections = replacement(document._collections)
    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    assert error.code == "business_snapshot_domain_state_invalid"


def test_capture_requires_reinstallable_validator_authority() -> None:
    document = _make_document(include_read_only=False)
    entity = document.get("card", "z-card")
    entity._validator = lambda data, schema, path: None
    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    assert error.code == "business_snapshot_validator_authority_mismatch"

    document = _make_document(include_read_only=False)
    entity = document.get("card", "z-card")
    entity._validator = make_validator({"type": "object"})
    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    assert error.code == "business_snapshot_validator_authority_mismatch"

    document = _make_document(include_read_only=False)
    entity = document.get("card", "z-card")
    entity._validator = _no_op_validator
    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    assert error.code == "business_snapshot_validator_authority_mismatch"


def test_capture_requires_every_entity_to_be_owned_by_the_captured_document() -> None:
    document = _make_document(include_read_only=True)
    canonical = document.get("card", "z-card")
    canonical._document_owner = Document(
        schema_version="1", collaboration_mode="live_collab"
    )
    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    assert error.code == "business_snapshot_document_owner_mismatch"

    document = _make_document(include_read_only=True)
    read_only = document.get("external", "read-only-1")
    read_only._document_owner = None
    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    assert error.code == "business_snapshot_document_owner_mismatch"


def test_capture_requires_valid_document_admission_origin_state() -> None:
    document = _make_document(include_read_only=False)
    entity = document.get("card", "z-card")
    entity._document_admission_state = None
    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    assert error.code == "business_snapshot_domain_state_invalid"


def test_empty_or_missing_document_schema_requires_exact_noop_validator() -> None:
    document = Document(
        id="document-id",
        schema_version="1",
        collaboration_mode="live_collab",
        schema={"empty": {}},
    )
    empty = Entity(type="empty", id="e", data={})
    missing = Entity(type="missing", id="m", data={})
    document.add(empty)
    document.add(missing)
    assert capture_business_snapshot("project-main", 1, document).snapshot.entities

    empty._validator = make_validator({})
    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    assert error.code == "business_snapshot_validator_authority_mismatch"

    empty._validator = _no_op_validator
    missing._validator = lambda data, schema, path: None
    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    assert error.code == "business_snapshot_validator_authority_mismatch"


def test_read_only_custom_validator_is_excluded_without_blocking_capture() -> None:
    document = _make_document(include_read_only=True)
    read_only = document.get("external", "read-only-1")
    read_only._validator = lambda data, schema, path: None
    result = capture_business_snapshot("project-main", 1, document)
    assert result.excluded_read_only == (
        EntityKey(entity_type="external", entity_id="read-only-1"),
    )


def test_collection_position_preserves_per_type_insertion_order() -> None:
    document = Document(
        id="ordered",
        schema_version="1",
        collaboration_mode="live_collab",
    )
    document.add(Entity(type="item", id="z", data={}))
    document.register_read_only_source(
        ReadOnlySourceSpec(
            name="overlay",
            entity_type="item",
            loader=lambda: [{"id": "ro", "data": {}}],
        )
    )
    document.add(Entity(type="item", id="a", data={}))
    document.add(Entity(type="item", id="m", data={}))
    document.add(Entity(type="other", id="b", data={}))
    snapshot = capture_business_snapshot("project-main", 1, document).snapshot
    assert [(row.entity_type, row.entity_id) for row in snapshot.entities] == [
        ("item", "a"),
        ("item", "m"),
        ("item", "z"),
        ("other", "b"),
    ]
    positions = {
        (row.entity_type, row.entity_id): decode_payload(row.snapshot_bytes)["state"][
            "collection_position"
        ]
        for row in snapshot.entities
    }
    assert positions == {
        ("item", "z"): 0,
        ("item", "a"): 1,
        ("item", "m"): 2,
        ("other", "b"): 0,
    }
    restored = restore_business_snapshot(snapshot)
    assert [entity.id for entity in restored.document.list("item")] == ["z", "a", "m"]


def test_collection_position_includes_soft_delete_and_closes_after_hard_remove() -> None:
    document = Document(
        id="ordered-delete",
        schema_version="1",
        collaboration_mode="live_collab",
    )
    actor = Actor(user_id="u")
    for entity_id in ("z", "a", "m"):
        document.add(Entity(type="item", id=entity_id, data={}))
    deleted = document.get("item", "a")
    deleted.delete(expected_version=deleted.version, actor=actor)
    document.hard_remove("item", "z")
    snapshot = capture_business_snapshot("project-main", 1, document).snapshot
    positions = {
        row.entity_id: decode_payload(row.snapshot_bytes)["state"][
            "collection_position"
        ]
        for row in snapshot.entities
    }
    assert positions == {"a": 0, "m": 1}
    restored = restore_business_snapshot(snapshot).document
    assert [entity.id for entity in restored.list("item", include_deleted=True)] == [
        "a",
        "m",
    ]
    assert restored.get("item", "a").deleted is True


@pytest.mark.parametrize("positions", [(0, 0, 2), (0, 2, 3), (True, 1, 2), (-1, 1, 2)])
def test_verify_rejects_duplicate_gap_or_invalid_collection_positions(
    positions,
) -> None:
    # Add a third card so duplicate/gap matrices are unambiguous.
    document = _make_document(include_read_only=False)
    document.add(Entity(type="card", id="middle", data={"title": "M", "score": 2}))
    snapshot = capture_business_snapshot("project-main", 1, document).snapshot
    card_indexes = [
        index for index, row in enumerate(snapshot.entities) if row.entity_type == "card"
    ]
    rows = list(snapshot.entities)
    for index, position in zip(card_indexes, positions):
        payload = decode_payload(rows[index].snapshot_bytes)
        payload["state"]["collection_position"] = position
        rows[index] = _row_with_payload(rows[index], encode_payload(payload))
    changed = _rebind_project(replace(snapshot, entities=tuple(rows)))
    error = _assert_snapshot_error(lambda: verify_business_snapshot(changed))
    assert error.code == "business_snapshot_collection_position_invalid"


def test_wrong_project_entity_count_rejects_before_entity_decode(monkeypatch) -> None:
    snapshot = _capture().snapshot
    payload = decode_payload(snapshot.project.snapshot_bytes)
    payload["state"]["entities"]["count"] += 1
    changed = replace(
        snapshot,
        project=_row_with_payload(snapshot.project, encode_payload(payload)),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Entity decode ran before root count rejection")

    monkeypatch.setattr(snapshot_module, "_verify_entity_row", forbidden)
    error = _assert_snapshot_error(lambda: verify_business_snapshot(changed))
    assert error.code == "business_snapshot_root_mismatch"
    assert error.path == "$.project.state.entities"


def test_snapshot_node_budget_maps_to_stable_business_error(monkeypatch) -> None:
    document = _make_document(include_read_only=False)
    monkeypatch.setattr(snapshot_module, "MAX_SNAPSHOT_VALUE_NODES", 1)
    error = _assert_snapshot_error(
        lambda: capture_business_snapshot("project-main", 1, document)
    )
    assert error.code == "business_snapshot_payload_size_invalid"
    assert error.path == "$.document"


def test_zero_entity_aggregate_budget_is_checked_before_return(monkeypatch) -> None:
    document = Document(
        id="empty",
        schema_version="1",
        collaboration_mode="live_collab",
    )
    snapshot = capture_business_snapshot("project-main", 1, document).snapshot
    combined = len(snapshot.project.snapshot_bytes) + len(snapshot.document.snapshot_bytes)
    monkeypatch.setattr(snapshot_module, "MAX_TOTAL_SNAPSHOT_BYTES", combined - 1)
    error = _assert_snapshot_error(lambda: verify_business_snapshot(snapshot))
    assert error.code == "business_snapshot_total_size_exceeded"


def test_entity_set_v1_hash_domain_has_a_fixed_empty_vector() -> None:
    document = Document(
        id="empty",
        schema_version="1",
        collaboration_mode="live_collab",
    )
    snapshot = capture_business_snapshot("project-main", 1, document).snapshot
    root = decode_payload(snapshot.project.snapshot_bytes)["state"]["entities"]
    assert root == {
        "count": 0,
        "set_sha256": "b9a83043963719d7355d4e97795a2608d3a7455b6580078eba5f7c68c937779c",
    }
    assert root["set_sha256"] == _entity_set_sha256(())
