"""test_document.py — Document 全实装行为测试.

覆盖:
  - 构造必填校验 (schema_version / collaboration_mode)
  - 元属性 property (含 acl / version)
  - _v 版本号 + 409 冲突检测
  - acl 字段
  - register_read_only_source 真 load entity
  - read_only_sources 列名
  - save/open round-trip (v2 identity-hash generation + 安全 legacy 迁移)
  - entity 集成: add/get/find/list/types/hard_remove/detect_cycles/cascade_delete
  - DocumentRegistry: declare / build_document / demo bootstrap 完整流转
"""

from __future__ import annotations

import copy
import gc
import json
import multiprocessing
import os
import threading
import time
import weakref
from pathlib import Path

import pytest
import document as document_module

from document import (
    Document,
    DocumentHead,
    DocumentMetadata,
    DocumentMutationSession,
    DocumentMutationSnapshot,
    DocumentRegistry,
    ReadOnlySourceSpec,
    ZeError,
    HISTORY_REPLAY_SCHEMA_GUARD,
    _HISTORY_REPLAY_CAPABILITY,
    _LEGACY_SCAN_DOCUMENT_CAPABILITY,
    _TOPOLOGY_STAGING_METADATA_CAPABILITY,
    _open_document_for_legacy_scan,
    _rewrite_document_metadata_for_topology_staging,
    open_document_head,
    proven_document_write_outcome,
    read_document_metadata,
)
from entity import Actor, AiProvenance, Entity, _no_op_validator
from field_types import is_framework_validator, make_validator
from entity_file_store import (
    ENTITY_STORE_DIRECTORY,
    EntityFileStore,
    EntityFileStoreError,
    entity_identity_digest,
)


def _v2_entity_path(document_path: Path, entity_type: str, entity_id: str) -> Path:
    raw = json.loads(document_path.read_text(encoding="utf-8"))
    digest = entity_identity_digest(entity_type, entity_id)
    return (
        document_path.parent
        / "entities"
        / ENTITY_STORE_DIRECTORY
        / str(raw["_v"])
        / digest[:2]
        / (digest + ".json")
    )


def _fork_atomic_candidate_probe(document, session, candidate, queue) -> None:
    try:
        document_module._verify_atomic_document_commit_candidate(
            candidate,
            document,
            session,
            _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
        )
    except BaseException as exc:  # pragma: no cover - asserted in parent
        queue.put(("error", getattr(exc, "code", type(exc).__name__)))
    else:  # pragma: no cover - must never happen
        queue.put(("accepted", ""))


def _multiprocess_same_head_save(
    path: str,
    value: int,
    ready,
    start,
    results,
) -> None:
    """Spawn-safe worker: both peers open before either is allowed to save."""
    try:
        document = Document.open(path)
        entity = document.get("item", "row")
        entity.update(
            {"value": value},
            expected_version=entity.version,
            actor=Actor(user_id="worker-%s" % value),
        )
        ready.put(document.version)
        if not start.wait(10):
            results.put(("worker_timeout", value))
            return
        document.save(path)
    except ZeError as exc:
        results.put((
            "error",
            exc.code,
            exc.context.get("reason"),
            exc.context.get("current"),
            exc.context.get("expected"),
        ))
    except BaseException as exc:  # pragma: no cover - diagnostic for child process
        results.put(("unexpected", type(exc).__name__, str(exc)))
    else:
        results.put(("saved", value, document.version))


def _multiprocess_paused_reader(path: str, inside, release, results) -> None:
    real_read = EntityFileStore.read_v2_entity

    def pause_before_entity_read(store, version, entity_type, entity_id):
        inside.set()
        if not release.wait(10):
            raise RuntimeError("reader release timed out")
        return real_read(store, version, entity_type, entity_id)

    EntityFileStore.read_v2_entity = pause_before_entity_read
    try:
        document = Document.open(path)
        results.put((
            "read",
            document.version,
            document.get("item", "row").data["value"],
        ))
    except BaseException as exc:  # pragma: no cover - child diagnostic
        results.put(("unexpected", type(exc).__name__, str(exc)))


def _multiprocess_writer_behind_reader(
    path: str,
    ready,
    start,
    attempting,
    done,
    results,
) -> None:
    try:
        document = Document.open(path)
        entity = document.get("item", "row")
        entity.update(
            {"value": 1},
            expected_version=entity.version,
            actor=Actor(user_id="cleanup-writer"),
        )
        ready.set()
        if not start.wait(10):
            raise RuntimeError("writer start timed out")
        attempting.set()
        document.save(path)
        results.put(("saved", document.version))
    except BaseException as exc:  # pragma: no cover - child diagnostic
        results.put(("unexpected", type(exc).__name__, str(exc)))
    finally:
        done.set()


# ---- 构造 + 必填校验 ----

def test_construct_minimal():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    assert doc.schema_version == "1.0.0"
    assert doc.collaboration_mode == "with_save"
    assert isinstance(doc.id, str) and len(doc.id) == 32  # uuid4 hex
    assert doc.name == ""
    assert doc.schema == {}
    assert doc.config == {}


def test_construct_with_all_fields():
    doc = Document(
        schema_version="2.0.0",
        collaboration_mode="live_collab",
        name="my-project",
        id="fixed-id-123",
        schema={"object": {"name": {"type": "string"}}},
        config={"canvas_size": [1920, 1080]},
    )
    assert doc.id == "fixed-id-123"
    assert doc.name == "my-project"
    assert doc.collaboration_mode == "live_collab"
    assert doc.schema == {"object": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }}
    assert doc.config == {"canvas_size": [1920, 1080]}


@pytest.mark.parametrize("bad", [[], 0, "", None])
def test_document_schema_rejects_falsey_non_object_entity_type_values(bad):
    with pytest.raises(ZeError) as exc:
        Document(
            schema_version="1.0.0",
            collaboration_mode="with_save",
            schema={"card": bad},
        )
    assert exc.value.code == "validation_failed"
    assert exc.value.context["reason"] == "document_schema_type_value_invalid"


@pytest.mark.parametrize("entity_type", ["", "__document__", "\ud800"])
def test_document_schema_rejects_invalid_entity_type_names(entity_type):
    with pytest.raises(ZeError) as exc:
        Document(
            schema_version="1.0.0",
            collaboration_mode="with_save",
            schema={entity_type: {}},
        )
    assert exc.value.code == "validation_failed"


def test_document_canonicalizes_legacy_flat_schema_at_ingress():
    source = {"card": {"title": {"type": "string", "required": True}}}
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema=source,
    )
    assert doc.schema == {"card": {
        "type": "object",
        "properties": {"title": {"type": "string", "required": True}},
    }}
    source["card"]["title"]["type"] = "integer"
    assert doc.schema["card"]["properties"]["title"]["type"] == "string"


def test_schema_version_required_empty():
    with pytest.raises(ZeError) as exc:
        Document(schema_version="", collaboration_mode="with_save")
    assert exc.value.code == "validation_failed"


def test_schema_version_required_none():
    with pytest.raises(ZeError):
        Document(schema_version=None, collaboration_mode="with_save")  # type: ignore[arg-type]


def test_collaboration_mode_required():
    with pytest.raises(ZeError) as exc:
        Document(schema_version="1.0.0", collaboration_mode="invalid_mode")
    assert exc.value.code == "validation_failed"


def test_collaboration_mode_accepts_live_collab():
    doc = Document(schema_version="1.0.0", collaboration_mode="live_collab")
    assert doc.collaboration_mode == "live_collab"


def test_collaboration_mode_accepts_with_save():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    assert doc.collaboration_mode == "with_save"


def test_collaboration_mode_accepts_realtime_checkpoint():
    doc = Document(schema_version="1.0.0", collaboration_mode="realtime_checkpoint")
    assert doc.collaboration_mode == "realtime_checkpoint"


@pytest.mark.parametrize("policy", ["auto_commit", "checkpoint_commit", "legacy"])
def test_save_policy_accepts_declared_values(policy: str):
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="live_collab",
        save_policy=policy,
    )
    assert doc.save_policy == policy


def test_save_policy_rejects_manual_and_unknown_values():
    for policy in ("manual", "manual_save", "unknown"):
        with pytest.raises(ZeError) as exc:
            Document(
                schema_version="1.0.0",
                collaboration_mode="live_collab",
                save_policy=policy,
            )
        assert exc.value.code == "validation_failed"


@pytest.mark.parametrize(
    ("declared", "mode", "effective", "auto_commit"),
    [
        ("auto_commit", "live_collab", "auto_commit", True),
        ("checkpoint_commit", "live_collab", "checkpoint_commit", True),
        (None, "with_save", "auto_commit", True),
        ("legacy", "with_save", "auto_commit", True),
        (None, "live_collab", "auto_commit", True),
        ("legacy", "live_collab", "auto_commit", True),
        (None, "realtime_checkpoint", "auto_commit", True),
    ],
)
def test_effective_save_policy_never_allows_oplog_only_success(
    declared: str | None,
    mode: str,
    effective: str,
    auto_commit: bool,
):
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode=mode,
        save_policy=declared,
    )
    assert doc.effective_save_policy == effective
    assert doc.auto_commit_enabled is auto_commit
    assert doc.should_auto_commit is auto_commit


@pytest.mark.parametrize("declared", [None, "legacy"])
def test_adopt_save_policy_migrates_only_missing_or_legacy(declared: str | None):
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="live_collab",
        save_policy=declared,
    )
    assert doc.adopt_save_policy("checkpoint_commit") is True
    assert doc.save_policy == "checkpoint_commit"
    assert doc.adopt_save_policy("checkpoint_commit") is False


def test_adopt_save_policy_does_not_override_explicit_current_policy():
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
    )
    assert doc.adopt_save_policy("checkpoint_commit") is False
    assert doc.save_policy == "auto_commit"


def test_adopt_save_policy_rejects_legacy_target():
    doc = Document(schema_version="1.0.0", collaboration_mode="live_collab")
    with pytest.raises(ZeError) as exc:
        doc.adopt_save_policy("legacy")
    assert exc.value.code == "validation_failed"


# ---- 元属性是 property (返回 copy, 不暴露内部) ----

def test_schema_returns_copy():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save",
                   schema={"a": {"value": {"type": "integer"}}})
    s = doc.schema
    s["b"] = {}
    assert doc.schema == {"a": {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
    }}  # 内部未被改


def test_config_returns_copy():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save",
                   config={"x": 1})
    c = doc.config
    c["y"] = 2
    assert doc.config == {"x": 1}


def test_timestamps_initialized():
    before = time.time()
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    after = time.time()
    assert before <= doc.created_at <= after
    assert doc.created_at == doc.updated_at


# ---- read_only_sources (M1: 仅记录 spec) ----

def test_register_read_only_source():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    spec = ReadOnlySourceSpec(
        name="external_levels",
        entity_type="level",
        loader=lambda: [],  # M1 不调
        schema={},
    )
    doc.register_read_only_source(spec)
    assert doc.read_only_sources() == ["external_levels"]


def test_register_read_only_source_duplicate_raises():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    spec = ReadOnlySourceSpec(name="src1", entity_type="t", loader=lambda: [])
    doc.register_read_only_source(spec)
    with pytest.raises(ZeError) as exc:
        doc.register_read_only_source(spec)
    assert exc.value.code == "validation_failed"


def test_register_read_only_source_calls_loader():
    """register 时调 loader 把返回的 dict 灌成 read_only entity."""
    def loader():
        return [
            {"id": "level1", "name": "Lv 1", "data": {"title": "Level 1"}},
            {"id": "level2", "name": "Lv 2", "data": {"title": "Level 2"}},
        ]
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.register_read_only_source(
        ReadOnlySourceSpec(name="external_levels", entity_type="level", loader=loader)
    )
    levels = doc.list("level")
    assert len(levels) == 2
    assert all(e.read_only for e in levels)
    assert {e.id for e in levels} == {"level1", "level2"}


def test_read_only_source_cannot_overwrite_canonical_entity():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    canonical = doc.add(Entity(
        type="level", id="same", data={"owner": "canonical"},
    ))

    with pytest.raises(ZeError) as captured:
        doc.register_read_only_source(ReadOnlySourceSpec(
            name="external",
            entity_type="level",
            loader=lambda: [
                {"id": "new", "data": {"owner": "overlay"}},
                {"id": "same", "data": {"owner": "overlay"}},
            ],
        ))

    assert captured.value.code == "validation_failed"
    assert captured.value.context["reason"] == "read_only_source_identity_conflict"
    assert doc.get("level", "same") is canonical
    assert doc.get("level", "same").data == {"owner": "canonical"}
    assert doc.find("level", "new") is None
    assert doc.read_only_sources() == []


def test_read_only_source_batch_is_atomic_on_duplicate_or_loader_failure():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    with pytest.raises(ZeError) as duplicate:
        doc.register_read_only_source(ReadOnlySourceSpec(
            name="duplicate",
            entity_type="level",
            loader=lambda: [{"id": "x"}, {"id": "x"}],
        ))
    assert duplicate.value.code == "validation_failed"
    assert duplicate.value.context["reason"] == "read_only_source_identity_conflict"
    assert doc.list("level") == []
    assert doc.read_only_sources() == []

    def broken_loader():
        yield {"id": "first"}
        raise RuntimeError("loader failed")

    with pytest.raises(RuntimeError, match="loader failed"):
        doc.register_read_only_source(ReadOnlySourceSpec(
            name="broken", entity_type="level", loader=broken_loader,
        ))
    assert doc.list("level") == []
    assert doc.read_only_sources() == []


def test_read_only_source_batch_is_atomic_on_late_schema_failure():
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={
            "level": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "required": True, "minLength": 1},
                },
            },
        },
    )

    with pytest.raises(ZeError) as captured:
        doc.register_read_only_source(ReadOnlySourceSpec(
            name="invalid-late",
            entity_type="level",
            loader=lambda: [
                {"id": "valid", "data": {"title": "ok"}},
                {"id": "invalid", "data": {"title": ""}},
            ],
        ))

    assert captured.value.code == "schema_violation"
    assert doc.list("level") == []
    assert doc.read_only_sources() == []


def test_read_only_source_streams_reused_row_buffer_without_aliasing():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    row: dict[str, object] = {}
    nested: dict[str, object] = {}
    items: list[object] = []

    def streaming_loader():
        for entity_id in ("first", "second"):
            row.clear()
            nested.clear()
            nested.update({"value": entity_id})
            items.clear()
            items.append(nested)
            row.update({"id": entity_id, "data": {"nested": nested, "items": items}})
            yield row

    doc.register_read_only_source(ReadOnlySourceSpec(
        name="stream", entity_type="level", loader=streaming_loader,
    ))

    assert [entity.id for entity in doc.list("level")] == ["first", "second"]
    first = doc.get("level", "first").data
    second = doc.get("level", "second").data
    assert first == {"nested": {"value": "first"}, "items": [{"value": "first"}]}
    assert second == {"nested": {"value": "second"}, "items": [{"value": "second"}]}
    assert first["nested"] is not second["nested"]
    assert first["items"] is not second["items"]


def test_read_only_source_rejects_custom_nested_values_without_deepcopy_hooks():
    calls: list[str] = []

    class DeepcopyTrap(dict):
        def __deepcopy__(self, memo):
            calls.append("called")
            raise AssertionError("extension deepcopy hook must not execute")

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    with pytest.raises(ZeError) as captured:
        doc.register_read_only_source(ReadOnlySourceSpec(
            name="unsafe",
            entity_type="level",
            loader=lambda: [{"id": "x", "data": {"nested": DeepcopyTrap()}}],
        ))

    assert captured.value.code == "validation_failed"
    assert captured.value.context["reason"] == "read_only_source_value_invalid"
    assert calls == []
    assert doc.read_only_sources() == []
    assert doc.list("level", include_deleted=True) == []


def _cyclic_read_only_row():
    value: dict[str, object] = {}
    value["self"] = value
    return {"id": "bad", "data": value}


def _too_deep_read_only_row():
    value: dict[str, object] = {}
    for _ in range(140):
        value = {"nested": value}
    return {"id": "bad", "data": value}


@pytest.mark.parametrize("row_factory", [
    pytest.param(lambda: {"id": "bad", "data": {"x": float("nan")}}, id="nan"),
    pytest.param(lambda: {"id": "bad", "data": {"x": float("inf")}}, id="infinity"),
    pytest.param(lambda: {"id": "bad", "data": {"x": "\ud800"}}, id="invalid-utf8"),
    pytest.param(_cyclic_read_only_row, id="cycle"),
    pytest.param(_too_deep_read_only_row, id="depth"),
    pytest.param(lambda: {"id": "bad", "data": {1: "x"}}, id="non-string-key"),
    pytest.param(lambda: {"id": "bad", "data": {"x": (1, 2)}}, id="tuple"),
])
def test_read_only_source_strict_json_rules_fail_closed(row_factory):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    canonical = doc.add(Entity(type="level", id="keep", data={"value": "safe"}))

    with pytest.raises(ZeError) as captured:
        doc.register_read_only_source(ReadOnlySourceSpec(
            name="invalid-json", entity_type="level", loader=lambda: [row_factory()],
        ))

    assert captured.value.code == "validation_failed"
    assert captured.value.context["reason"] == "read_only_source_value_invalid"
    assert doc.get("level", "keep") is canonical
    assert doc.find("level", "bad") is None
    assert doc.read_only_sources() == []


def test_read_only_source_schema_must_be_strict_json_even_when_document_schema_wins():
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"level": {}},
    )

    with pytest.raises(ZeError) as captured:
        doc.register_read_only_source(ReadOnlySourceSpec(
            name="invalid-schema",
            entity_type="level",
            loader=lambda: [],
            schema={"limit": float("nan")},
        ))

    assert captured.value.context["reason"] == "read_only_source_value_invalid"
    assert doc.read_only_sources() == []


@pytest.mark.parametrize(("row", "member"), [
    ({"id": 1}, "id"),
    ({"id": True}, "id"),
    ({"id": ""}, "id"),
    ({"name": []}, "name"),
    ({"data": [["coerced", 1]]}, "data"),
])
def test_read_only_source_rejects_invalid_row_members_without_coercion(row, member):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")

    with pytest.raises(ZeError) as captured:
        doc.register_read_only_source(ReadOnlySourceSpec(
            name="invalid-row", entity_type="level", loader=lambda: [row],
        ))

    assert captured.value.code == "validation_failed"
    assert captured.value.context == {
        "reason": "read_only_source_row_invalid",
        "source": "invalid-row",
        "row_index": 0,
        "member": member,
    }
    assert doc.read_only_sources() == []
    assert doc.list("level", include_deleted=True) == []


@pytest.mark.parametrize("entity_type", ["", "   ", "__document__"])
def test_read_only_source_rejects_invalid_type_even_when_loader_is_empty(entity_type):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")

    with pytest.raises(ZeError) as captured:
        doc.register_read_only_source(ReadOnlySourceSpec(
            name="invalid", entity_type=entity_type, loader=lambda: [],
        ))

    assert captured.value.code == "validation_failed"
    assert captured.value.context["reason"] == "read_only_source_entity_type_invalid"
    assert doc.read_only_sources() == []


@pytest.mark.parametrize(
    ("name", "entity_type", "reason"),
    [
        ("\ud800", "level", "read_only_source_name_invalid"),
        ("source", "\ud800", "read_only_source_entity_type_invalid"),
    ],
)
def test_read_only_source_rejects_non_utf8_declaration_strings_before_loader(
    name, entity_type, reason,
):
    calls = []

    def loader():
        calls.append("called")
        return []

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    with pytest.raises(ZeError) as captured:
        doc.register_read_only_source(ReadOnlySourceSpec(
            name=name,
            entity_type=entity_type,
            loader=loader,
        ))

    assert captured.value.code == "validation_failed"
    assert captured.value.context["reason"] == reason
    assert calls == []
    assert doc.read_only_sources() == []


def test_read_only_source_rejects_spec_subclass_without_attribute_hooks():
    hooks = []

    class SpecSubclass(ReadOnlySourceSpec):
        def __getattribute__(self, name):
            hooks.append(name)
            raise AssertionError("subclass attribute hook must not run")

    spec = object.__new__(SpecSubclass)
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")

    with pytest.raises(ZeError) as captured:
        doc.register_read_only_source(spec)

    assert captured.value.code == "validation_failed"
    assert hooks == []
    assert doc.read_only_sources() == []


def test_read_only_source_schema_is_fallback_but_document_schema_is_authority():
    fallback = Document(schema_version="1.0.0", collaboration_mode="with_save")
    with pytest.raises(ZeError) as invalid:
        fallback.register_read_only_source(ReadOnlySourceSpec(
            name="fallback",
            entity_type="level",
            loader=lambda: [{"id": "x", "data": {"title": ""}}],
            schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "required": True, "minLength": 1},
                },
            },
        ))
    assert invalid.value.code == "schema_violation"
    assert fallback.read_only_sources() == []

    authoritative = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={
            "level": {
                "type": "object",
                "properties": {"title": {"type": "string", "required": True}},
            },
        },
    )
    authoritative.register_read_only_source(ReadOnlySourceSpec(
        name="document-wins",
        entity_type="level",
        loader=lambda: [{"id": "x", "data": {"title": "valid"}}],
        schema={
            "type": "object",
            "properties": {"title": {"type": "number", "required": True}},
        },
    ))
    assert authoritative.get("level", "x").data == {"title": "valid"}


def test_read_only_sources_empty():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    assert doc.read_only_sources() == []


def test_read_only_sources_sorted():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    for name in ["zebra", "alpha", "mid"]:
        doc.register_read_only_source(
            ReadOnlySourceSpec(name=name, entity_type="t", loader=lambda: [])
        )
    assert doc.read_only_sources() == ["alpha", "mid", "zebra"]


# ---- save / open (M1: 元数据 + 空 collections) ----

def test_save_writes_document_json(tmp_path: Path):
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        name="proj",
        id="doc-id-1",
        schema={"object": {"name": {"type": "string"}}},
        config={"k": "v"},
    )
    path = tmp_path / "document.json"
    doc.save(path)

    assert path.exists()
    raw = json.loads(path.read_text())
    assert raw["schema"] == "ze-document"
    assert raw["schema_version"] == "1.0.0"
    assert raw["id"] == "doc-id-1"
    assert raw["name"] == "proj"
    assert raw["collaboration_mode"] == "with_save"
    assert raw["data_schema"] == {"object": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }}
    assert raw["config"] == {"k": "v"}
    assert raw["collections"] == {}  # M1: 空索引
    assert raw["read_only_sources"] == []


def test_save_safely_creates_nested_parent_dirs(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    path = tmp_path / "nested" / "deep" / "document.json"
    doc.save(path)
    assert path.exists()


def test_save_includes_read_only_sources(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.register_read_only_source(
        ReadOnlySourceSpec(name="src1", entity_type="lvl", loader=lambda: [])
    )
    path = tmp_path / "document.json"
    doc.save(path)
    raw = json.loads(path.read_text())
    assert raw["read_only_sources"] == [{"name": "src1", "entity_type": "lvl"}]


def test_save_keeps_read_only_materialization_out_of_canonical_document(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="owned", id="local-1", data={"value": 1}))
    doc.register_read_only_source(ReadOnlySourceSpec(
        name="external-levels",
        entity_type="level",
        loader=lambda: [{"id": "remote-1", "data": {"value": 99}}],
    ))
    assert doc.get("level", "remote-1").read_only is True

    path = tmp_path / "document.json"
    doc.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["collections"] == {"owned": ["local-1"]}
    assert raw["read_only_sources"] == [
        {"name": "external-levels", "entity_type": "level"},
    ]
    assert not (tmp_path / "entities" / "level" / "remote-1.json").exists()
    reopened = Document.open(path)
    assert reopened.find("owned", "local-1") is not None
    assert reopened.find("level", "remote-1") is None


def test_save_removes_legacy_persisted_read_only_rows(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    legacy = Entity(type="level", id="legacy-remote", data={"value": 3}, read_only=True)
    doc._collections["level"][legacy.id] = legacy
    entity_path = tmp_path / "entities" / "level" / "legacy-remote.json"
    entity_path.parent.mkdir(parents=True)
    entity_path.write_text(json.dumps(legacy.to_dict()), encoding="utf-8")

    doc.save(tmp_path / "document.json")

    assert not entity_path.exists()
    raw = json.loads((tmp_path / "document.json").read_text(encoding="utf-8"))
    assert "level" not in raw["collections"]


def test_open_legacy_mixed_index_restores_canonical_then_overlay_order(tmp_path: Path):
    """legacy 索引即使把只读行写在前面，内存序也必须回到 canonical + overlay。"""
    path = tmp_path / "document.json"
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="card", id="z", data={"value": "z"}))
    doc.add(Entity(type="card", id="a", data={"value": "a"}))
    doc.save(path)

    read_only = Entity(
        type="card", id="ro", data={"value": "remote"}, read_only=True,
    )
    read_only_path = tmp_path / "entities" / "card" / "ro.json"
    read_only_path.parent.mkdir(parents=True)
    for entity in (doc.get("card", "z"), doc.get("card", "a"), read_only):
        (read_only_path.parent / (entity.id + ".json")).write_text(
            json.dumps(entity.to_dict(), ensure_ascii=False), encoding="utf-8",
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["entity_storage"]
    raw["collections"]["card"] = ["ro", "z", "a"]
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    reopened = Document.open(path)

    assert [entity.id for entity in reopened.list("card")] == ["z", "a", "ro"]
    reopened.save(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["collections"]["card"] == ["z", "a"]
    assert not read_only_path.exists()


def test_save_bumps_updated_at(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    original_updated = doc.updated_at
    time.sleep(0.01)
    doc.save(tmp_path / "document.json")
    assert doc.updated_at > original_updated


def test_save_bumps_version_on_success_but_not_on_failure(tmp_path: Path, monkeypatch):
    """成功 save → _v +1;document.json 写盘失败 → _v 不前进(簇 C-5,防内存版本超盘一版)。"""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    v0 = doc.version
    doc.save(tmp_path / "document.json")
    assert doc.version == v0 + 1                       # 成功:版本前进
    v1 = doc.version

    def boom(_store, _payload):
        raise EntityFileStoreError("document_write_failed", "disk_full")

    monkeypatch.setattr(EntityFileStore, "commit_document", boom)
    with pytest.raises(ZeError) as captured:
        doc.save(tmp_path / "document.json")
    assert captured.value.code == "document_write_failed"
    assert doc.version == v1                            # 失败:版本不前进(写盘成功后才提交)


def test_open_roundtrip(tmp_path: Path):
    original = Document(
        schema_version="1.5.0",
        collaboration_mode="live_collab",
        name="proj",
        id="fixed-id",
        schema={"a": {"value": {"type": "integer"}}},
        config={"b": 2},
    )
    path = tmp_path / "document.json"
    original.save(path)

    reopened = Document.open(path)
    assert reopened.id == "fixed-id"
    assert reopened.name == "proj"
    assert reopened.schema_version == "1.5.0"
    assert reopened.collaboration_mode == "live_collab"
    assert reopened.schema == {"a": {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
    }}
    assert reopened.config == {"b": 2}


@pytest.mark.parametrize("bad", [[], 0, "", None])
def test_open_rejects_falsey_non_object_entity_schema_values(tmp_path: Path, bad):
    path = tmp_path / "document.json"
    Document(
        schema_version="1.0.0", collaboration_mode="with_save"
    ).save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["data_schema"] = {"card": bad}
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ZeError) as exc:
        Document.open(path)
    assert exc.value.code == "project_recovery_required"


@pytest.mark.parametrize("bad", [[], 0, "", None])
def test_open_rejects_explicit_falsey_non_object_config(tmp_path: Path, bad):
    path = tmp_path / "document.json"
    Document(
        schema_version="1.0.0", collaboration_mode="with_save"
    ).save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["config"] = bad
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ZeError) as exc:
        Document.open(path)
    assert exc.value.code == "project_recovery_required"


def test_open_defaults_only_a_missing_config(tmp_path: Path):
    path = tmp_path / "document.json"
    Document(
        schema_version="1.0.0", collaboration_mode="with_save"
    ).save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["config"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert Document.open(path).config == {}


def test_open_canonicalizes_legacy_flat_schema_and_next_save_migrates_it(tmp_path: Path):
    path = tmp_path / "document.json"
    Document(
        schema_version="1.0.0", collaboration_mode="with_save"
    ).save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["data_schema"] = {"card": {"title": {"type": "string"}}}
    path.write_text(json.dumps(raw), encoding="utf-8")

    reopened = Document.open(path)
    assert reopened.schema["card"] == {
        "type": "object",
        "properties": {"title": {"type": "string"}},
    }
    reopened.save(path)
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["data_schema"] == reopened.schema


@pytest.mark.parametrize("policy", ["auto_commit", "checkpoint_commit", "legacy"])
def test_save_policy_roundtrip_when_explicit(tmp_path: Path, policy: str):
    original = Document(
        schema_version="1.0.0",
        collaboration_mode="live_collab",
        save_policy=policy,
    )
    path = tmp_path / "document.json"
    original.save(path)

    raw = json.loads(path.read_text())
    assert raw["save_policy"] == policy
    reopened = Document.open(path)
    assert reopened.save_policy == policy
    assert reopened.effective_save_policy == (
        policy if policy != "legacy" else "auto_commit"
    )


@pytest.mark.parametrize(
    ("mode", "effective", "auto_commit"),
    [
        ("with_save", "auto_commit", True),
        ("live_collab", "auto_commit", True),
        ("realtime_checkpoint", "auto_commit", True),
    ],
)
def test_old_document_without_save_policy_stays_unmigrated_on_save(
    tmp_path: Path,
    mode: str,
    effective: str,
    auto_commit: bool,
):
    path = tmp_path / "document.json"
    original = Document(schema_version="1.0.0", collaboration_mode=mode)
    original.save(path)
    assert "save_policy" not in json.loads(path.read_text())

    reopened = Document.open(path)
    assert reopened.save_policy is None
    assert reopened.effective_save_policy == effective
    assert reopened.should_auto_commit is auto_commit

    reopened.save(path)
    assert "save_policy" not in json.loads(path.read_text())


def test_open_rejects_invalid_save_policy(tmp_path: Path):
    path = tmp_path / "document.json"
    doc = Document(schema_version="1.0.0", collaboration_mode="live_collab")
    doc.save(path)
    raw = json.loads(path.read_text())
    raw["save_policy"] = "manual"
    path.write_text(json.dumps(raw))

    with pytest.raises(ZeError) as exc:
        Document.open(path)
    assert exc.value.code == "project_recovery_required"


def test_open_preserves_timestamps(tmp_path: Path):
    original = Document(schema_version="1.0.0", collaboration_mode="with_save")
    original_created = original.created_at
    path = tmp_path / "document.json"
    original.save(path)
    saved_updated = original.updated_at

    reopened = Document.open(path)
    assert reopened.created_at == original_created
    assert reopened.updated_at == saved_updated


def test_open_missing_collaboration_mode_raises(tmp_path: Path):
    """坏 durable 元数据稳定进入 recovery，而不是泄漏内部 KeyError。"""
    path = tmp_path / "document.json"
    path.write_text(json.dumps({
        "schema_version": "1.0.0",
        "id": "id1",
        "name": "p",
    }))
    with pytest.raises(ZeError) as captured:
        Document.open(path)
    assert captured.value.code == "project_recovery_required"


# ---- acl 字段 ----

def test_acl_default_empty():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    assert doc.acl == {}


def test_acl_passed_in():
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        acl={"read": ["all"], "write": ["admin"]},
    )
    assert doc.acl == {"read": ["all"], "write": ["admin"]}


def test_acl_returns_copy():
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        acl={"read": ["all"]},
    )
    a = doc.acl
    a["write"] = ["admin"]
    assert doc.acl == {"read": ["all"]}


def test_acl_returns_deep_copy_so_nested_mutation_cannot_bypass_command_path():
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        acl={
            "owner": "owner",
            "collaborators": ["alice"],
            "rules": {"writers": ["owner"]},
        },
    )

    exposed = doc.acl
    exposed["collaborators"].append("mallory")
    exposed["rules"]["writers"].append("mallory")

    assert doc.acl == {
        "owner": "owner",
        "collaborators": ["alice"],
        "rules": {"writers": ["owner"]},
    }


def test_acl_roundtrip(tmp_path: Path):
    original = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        acl={"read": ["user1"], "write": ["admin"]},
    )
    path = tmp_path / "document.json"
    original.save(path)
    reopened = Document.open(path)
    assert reopened.acl == {"read": ["user1"], "write": ["admin"]}


def test_open_rejects_present_non_object_acl_instead_of_making_it_public(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    path = tmp_path / "document.json"
    doc.save(path)
    raw = json.loads(path.read_text())
    raw["acl"] = []
    path.write_text(json.dumps(raw))

    with pytest.raises(ZeError) as captured:
        Document.open(path)
    assert captured.value.code == "project_recovery_required"


def test_acl_rejects_string_collaborators_to_prevent_substring_authorization():
    with pytest.raises(ZeError) as captured:
        Document(
            schema_version="1.0.0",
            collaboration_mode="with_save",
            acl={"owner": "alice", "collaborators": "bob"},  # type: ignore[arg-type]
        )
    assert captured.value.code == "validation_failed"


# ---- _v 版本号 + 409 冲突检测 ----

def test_version_starts_at_1():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    assert doc.version == 1


def test_version_bumps_on_save(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.save(tmp_path / "document.json")
    assert doc.version == 2
    doc.save(tmp_path / "document.json")
    assert doc.version == 3


def test_version_persisted(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    path = tmp_path / "document.json"
    doc.save(path)
    doc.save(path)
    reopened = Document.open(path)
    assert reopened.version == 3  # bumped from 1→2→3


def test_save_with_correct_expected_version(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    # version starts at 1
    doc.save(tmp_path / "document.json", expected_version=1)
    assert doc.version == 2


def test_save_with_wrong_expected_version_raises(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.save(tmp_path / "document.json")  # now version=2
    with pytest.raises(ZeError) as exc:
        doc.save(tmp_path / "document.json", expected_version=99)
    assert exc.value.code == "version_conflict"


def test_save_expected_version_none_skips_check(tmp_path: Path):
    """expected_version=None (默认) 不做 409 校验, 总能 save."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.save(tmp_path / "document.json")
    doc.save(tmp_path / "document.json")  # 不传 expected_version, 应该过


def test_version_conflict_does_not_bump(tmp_path: Path):
    """409 冲突时 _v 不应该 +1."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    with pytest.raises(ZeError):
        doc.save(tmp_path / "document.json", expected_version=99)
    assert doc.version == 1  # 未 bump


def test_stale_durable_head_conflicts_before_same_generation_is_touched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    original = Document(
        id="doc-cas",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    original.add(Entity(type="item", id="row", data={"value": 0}))
    original.save(path)
    stale = Document.open(path)
    winner = Document.open(path)
    winner.get("item", "row").update(
        {"value": 1},
        expected_version=1,
        actor=_actor(),
    )
    winner.save(path)
    before_head = path.read_bytes()
    current_generation = path.parent / "entities" / ENTITY_STORE_DIRECTORY / "3"
    before_generation = {
        entry.relative_to(current_generation): entry.read_bytes()
        for entry in current_generation.rglob("*.json")
    }
    writes = 0
    real_write_generation = EntityFileStore.write_generation

    def tripwire(store, version, payloads):
        nonlocal writes
        writes += 1
        return real_write_generation(store, version, payloads)

    monkeypatch.setattr(EntityFileStore, "write_generation", tripwire)
    stale.get("item", "row").update(
        {"value": 2},
        expected_version=1,
        actor=_actor(),
    )
    with pytest.raises(ZeError) as captured:
        stale.save(path)

    assert captured.value.code == "version_conflict"
    assert captured.value.context == {
        "current": 3,
        "expected": 2,
        "reason": "durable_head_advanced",
        "commit_point_reached": False,
    }
    assert writes == 0
    assert stale.version == 2
    assert path.read_bytes() == before_head
    assert {
        entry.relative_to(current_generation): entry.read_bytes()
        for entry in current_generation.rglob("*.json")
    } == before_generation
    assert Document.open(path).get("item", "row").data == {"value": 1}


def test_save_rejects_missing_durable_head_identity_before_generation_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="strict-head-id",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["id"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    path.chmod(0o600)
    writes = 0

    def tripwire(*_args, **_kwargs):
        nonlocal writes
        writes += 1
        raise AssertionError("generation write must not run")

    monkeypatch.setattr(EntityFileStore, "write_generation", tripwire)
    with pytest.raises(ZeError) as captured:
        document.save(path)

    assert captured.value.code == "project_recovery_required"
    assert captured.value.context["reason"] == "durable_head_identity_mismatch"
    assert captured.value.context["commit_point_reached"] is False
    assert writes == 0
    assert document.version == 2


def test_real_processes_from_same_head_have_one_winner_and_one_conflict(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="doc-multiprocess",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.add(Entity(type="item", id="row", data={"value": 0}))
    document.save(path)

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_multiprocess_same_head_save,
            args=(str(path), value, ready, start, results),
        )
        for value in (10, 20)
    ]
    for process in processes:
        process.start()
    assert [ready.get(timeout=10) for _ in processes] == [2, 2]
    start.set()
    outcomes = [results.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    saved = [outcome for outcome in outcomes if outcome[0] == "saved"]
    conflicts = [outcome for outcome in outcomes if outcome[0] == "error"]
    assert len(saved) == 1
    assert saved[0][2] == 3
    assert conflicts == [("error", "version_conflict", "durable_head_advanced", 3, 2)]
    reopened = Document.open(path)
    assert reopened.version == 3
    assert reopened.get("item", "row").data["value"] == saved[0][1]
    generations = path.parent / "entities" / ENTITY_STORE_DIRECTORY
    assert {entry.name for entry in generations.iterdir()} == {"3"}


def test_real_reader_keeps_old_generation_reachable_until_open_finishes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="doc-reader-lock",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.add(Entity(type="item", id="row", data={"value": 0}))
    document.save(path)

    context = multiprocessing.get_context("spawn")
    writer_ready = context.Event()
    writer_start = context.Event()
    writer_attempting = context.Event()
    writer_done = context.Event()
    writer_results = context.Queue()
    reader_inside = context.Event()
    reader_release = context.Event()
    reader_results = context.Queue()
    writer = context.Process(
        target=_multiprocess_writer_behind_reader,
        args=(
            str(path),
            writer_ready,
            writer_start,
            writer_attempting,
            writer_done,
            writer_results,
        ),
    )
    reader = context.Process(
        target=_multiprocess_paused_reader,
        args=(str(path), reader_inside, reader_release, reader_results),
    )
    writer.start()
    assert writer_ready.wait(10)
    reader.start()
    assert reader_inside.wait(10)
    writer_start.set()
    assert writer_attempting.wait(10)
    assert writer_done.wait(0.15) is False
    assert (path.parent / "entities" / ENTITY_STORE_DIRECTORY / "2").exists()

    reader_release.set()
    assert reader_results.get(timeout=10) == ("read", 2, 0)
    assert writer_results.get(timeout=10) == ("saved", 3)
    for process in (reader, writer):
        process.join(timeout=10)
        assert process.exitcode == 0

    assert Document.open(path).get("item", "row").data == {"value": 1}
    generations = path.parent / "entities" / ENTITY_STORE_DIRECTORY
    assert {entry.name for entry in generations.iterdir()} == {"3"}


def test_legacy_scan_context_yields_exact_head_and_public_open_equivalent_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="scanner-exact",
        name="Scanner exact",
        schema_version="1.0.0",
        collaboration_mode="live_collab",
        schema={"card": {"value": {"type": "integer"}}},
        config={"nested": {"enabled": True}},
    )
    document.add(Entity(type="card", id="row", data={"value": 7}))
    document.save(path)
    exact_head = path.read_bytes()
    root_info = path.parent.stat()
    root_identity = (root_info.st_dev, root_info.st_ino)
    loader_limits: list[tuple[int | None, int | None]] = []
    real_loader = document_module._load_persisted_document_from_locked_store

    def observed_loader(
        document_class,
        store,
        head,
        *,
        maximum_raw_entity_bytes=None,
        maximum_entity_count=None,
    ):
        assert store._lock_mode == "shared"
        loader_limits.append((maximum_raw_entity_bytes, maximum_entity_count))
        return real_loader(
            document_class,
            store,
            head,
            maximum_raw_entity_bytes=maximum_raw_entity_bytes,
            maximum_entity_count=maximum_entity_count,
        )

    monkeypatch.setattr(
        document_module,
        "_load_persisted_document_from_locked_store",
        observed_loader,
    )
    publicly_opened = Document.open(path)
    with _open_document_for_legacy_scan(
        path,
        _expected_root_identity=root_identity,
        _capability=_LEGACY_SCAN_DOCUMENT_CAPABILITY,
    ) as (head, scanned):
        assert type(head) is DocumentHead
        assert head.payload == exact_head
        assert head.metadata.id == "scanner-exact"
        assert head.metadata.version == document.version
        assert type(scanned) is Document
        assert scanned.id == publicly_opened.id
        assert scanned.version == publicly_opened.version
        assert scanned.config == publicly_opened.config
        assert scanned.get("card", "row").to_dict() == (
            publicly_opened.get("card", "row").to_dict()
        )

    assert loader_limits == [
        (None, None),
        (
            document_module._MAX_LEGACY_SCAN_RAW_ENTITY_BYTES,
            document_module._MAX_LEGACY_SCAN_ENTITY_COUNT,
        ),
    ]


def test_legacy_scan_authority_and_root_proof_fail_before_store_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    root_info = tmp_path.stat()
    root_identity = (root_info.st_dev, root_info.st_ino)
    store_calls = 0

    def forbidden_store(*_args, **_kwargs):
        nonlocal store_calls
        store_calls += 1
        raise AssertionError("invalid scanner authority must perform zero store I/O")

    monkeypatch.setattr(document_module, "EntityFileStore", forbidden_store)
    cases = (
        (object(), root_identity, "scan_capability_required"),
        (_LEGACY_SCAN_DOCUMENT_CAPABILITY, None, "scan_root_identity_required"),
        (_LEGACY_SCAN_DOCUMENT_CAPABILITY, (True, 1), "scan_root_identity_required"),
        (_LEGACY_SCAN_DOCUMENT_CAPABILITY, (1, -1), "scan_root_identity_required"),
    )
    for capability, expected_root, reason in cases:
        with pytest.raises(ZeError) as captured:
            with _open_document_for_legacy_scan(
                path,
                _expected_root_identity=expected_root,  # type: ignore[arg-type]
                _capability=capability,
            ):
                raise AssertionError("invalid scanner authority yielded")
        assert captured.value.code == "legacy_document_scan_forbidden"
        assert captured.value.context == {"reason": reason}
        assert str(tmp_path) not in str(captured.value)
    assert store_calls == 0


def test_legacy_scan_wrong_root_identity_performs_zero_content_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="scanner-root-pin",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.add(Entity(type="card", id="row", data={"value": 1}))
    document.save(path)
    root_info = path.parent.stat()
    root_identity = (root_info.st_dev, root_info.st_ino)
    calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        calls.append("content")
        raise AssertionError("a mismatched root must fail before content reads")

    monkeypatch.setattr(EntityFileStore, "acquire_shared_lock", forbidden)
    monkeypatch.setattr(EntityFileStore, "read_document", forbidden)
    monkeypatch.setattr(EntityFileStore, "read_v2_entity", forbidden)
    monkeypatch.setattr(EntityFileStore, "read_legacy_entity", forbidden)
    with pytest.raises(ZeError) as captured:
        with _open_document_for_legacy_scan(
            path,
            _expected_root_identity=(root_identity[0], root_identity[1] + 1),
            _capability=_LEGACY_SCAN_DOCUMENT_CAPABILITY,
        ):
            raise AssertionError("mismatched root yielded")
    assert captured.value.code == "document_storage_path_unsafe"
    assert captured.value.context["reason"] == "project_directory_identity_mismatch"
    assert calls == []

    with pytest.raises(ZeError) as head_error:
        with open_document_head(
            path,
            _expected_root_identity=(root_identity[0], root_identity[1] + 1),
        ):
            raise AssertionError("mismatched head root yielded")
    assert head_error.value.code == "document_storage_path_unsafe"
    assert head_error.value.context["reason"] == "project_directory_identity_mismatch"
    assert calls == []

    monkeypatch.undo()
    with open_document_head(
        path,
        _expected_root_identity=root_identity,
    ) as head:
        assert head is not None
        assert head.metadata.id == "scanner-root-pin"


def test_legacy_scan_context_uses_one_store_and_holds_root_and_shared_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    Document(
        id="scanner-held-root",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    ).save(path)
    root_info = path.parent.stat()
    root_identity = (root_info.st_dev, root_info.st_ino)
    instances: list[EntityFileStore] = []

    class ObservedStore(EntityFileStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

    monkeypatch.setattr(document_module, "EntityFileStore", ObservedStore)
    with _open_document_for_legacy_scan(
        path,
        _expected_root_identity=root_identity,
        _capability=_LEGACY_SCAN_DOCUMENT_CAPABILITY,
    ) as (_head, scanned):
        assert scanned.id == "scanner-held-root"
        assert len(instances) == 1
        assert instances[0]._root_fd >= 0
        assert instances[0]._lock_fd >= 0
        assert instances[0]._lock_mode == "shared"
        held_info = os.fstat(instances[0]._root_fd)
        assert (held_info.st_dev, held_info.st_ino) == root_identity

    assert len(instances) == 1
    assert instances[0]._root_fd == -1
    assert instances[0]._lock_fd == -1
    assert instances[0]._lock_mode is None


def test_legacy_scan_raw_entity_budget_is_aggregate_and_does_not_change_public_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="scanner-budget",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.add(Entity(type="card", id="one", data={"value": "a" * 32}))
    document.add(Entity(type="card", id="two", data={"value": "b" * 32}))
    document.save(path)
    payload_sizes = [
        len(_v2_entity_path(path, "card", entity_id).read_bytes())
        for entity_id in ("one", "two")
    ]
    total = sum(payload_sizes)
    assert total - 1 > max(payload_sizes)
    root_info = path.parent.stat()
    root_identity = (root_info.st_dev, root_info.st_ino)
    real_read = EntityFileStore.read_v2_entity
    read_limits: list[tuple[str, int | None]] = []

    def observed_read(
        self,
        version,
        entity_type,
        entity_id,
        *,
        _maximum_bytes=None,
    ):
        read_limits.append((entity_id, _maximum_bytes))
        return real_read(
            self,
            version,
            entity_type,
            entity_id,
            _maximum_bytes=_maximum_bytes,
        )

    monkeypatch.setattr(EntityFileStore, "read_v2_entity", observed_read)

    monkeypatch.setattr(
        document_module,
        "_MAX_LEGACY_SCAN_RAW_ENTITY_BYTES",
        total,
    )
    with _open_document_for_legacy_scan(
        path,
        _expected_root_identity=root_identity,
        _capability=_LEGACY_SCAN_DOCUMENT_CAPABILITY,
    ) as (_head, scanned):
        assert [entity.id for entity in scanned.list("card")] == ["one", "two"]
    assert read_limits == [
        ("one", total),
        ("two", total - payload_sizes[0]),
    ]

    read_limits.clear()
    monkeypatch.setattr(
        document_module,
        "_MAX_LEGACY_SCAN_RAW_ENTITY_BYTES",
        total - 1,
    )
    with pytest.raises(ZeError) as captured:
        with _open_document_for_legacy_scan(
            path,
            _expected_root_identity=root_identity,
            _capability=_LEGACY_SCAN_DOCUMENT_CAPABILITY,
        ):
            raise AssertionError("over-budget scanner yielded")
    assert captured.value.code == "document_storage_layout_invalid"
    assert captured.value.context["reason"] == "legacy_scan_raw_entity_bytes_exceeded"
    assert str(tmp_path) not in str(captured.value)
    assert read_limits == [
        ("one", total - 1),
        ("two", total - 1 - payload_sizes[0]),
    ]
    assert read_limits[-1][1] == payload_sizes[1] - 1

    # The scanner-only ceiling must not silently narrow the long-standing public
    # Document.open compatibility contract.
    publicly_opened = Document.open(path)
    assert [entity.id for entity in publicly_opened.list("card")] == ["one", "two"]
    assert read_limits[-2:] == [("one", None), ("two", None)]


def test_legacy_scan_entity_count_budget_fails_before_entity_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="scanner-count-budget",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.add(Entity(type="card", id="one", data={"value": 1}))
    document.add(Entity(type="card", id="two", data={"value": 2}))
    document.save(path)
    root_info = path.parent.stat()
    monkeypatch.setattr(document_module, "_MAX_LEGACY_SCAN_ENTITY_COUNT", 1)
    read_calls = 0

    def forbidden_read(*_args, **_kwargs):
        nonlocal read_calls
        read_calls += 1
        raise AssertionError("entity-count preflight must run before Entity I/O")

    monkeypatch.setattr(EntityFileStore, "read_v2_entity", forbidden_read)
    with pytest.raises(ZeError) as captured:
        with _open_document_for_legacy_scan(
            path,
            _expected_root_identity=(root_info.st_dev, root_info.st_ino),
            _capability=_LEGACY_SCAN_DOCUMENT_CAPABILITY,
        ):
            raise AssertionError("over-count scanner yielded")
    assert captured.value.code == "document_storage_layout_invalid"
    assert captured.value.context == {"reason": "legacy_scan_entity_count_exceeded"}
    assert read_calls == 0

    monkeypatch.undo()
    assert len(Document.open(path).list("card")) == 2


def test_legacy_scan_context_reuses_safe_raw_layout_semantics(tmp_path: Path) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="scanner-raw",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.add(Entity(type="旧 类型", id="一 号", data={"value": 9}))
    document.save(path)
    legacy_path = tmp_path / "entities" / "旧 类型" / "一 号.json"
    legacy_path.parent.mkdir(exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            document.get("旧 类型", "一 号").to_dict(),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    legacy_path.chmod(0o644)
    legacy_path.parent.chmod(0o755)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["entity_storage"]
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    root_info = path.parent.stat()

    with _open_document_for_legacy_scan(
        path,
        _expected_root_identity=(root_info.st_dev, root_info.st_ino),
        _capability=_LEGACY_SCAN_DOCUMENT_CAPABILITY,
    ) as (head, scanned):
        assert head.metadata.storage_version == "legacy"
        assert scanned.get("旧 类型", "一 号").data == {"value": 9}


@pytest.mark.parametrize("bad", [True, 1.0, "1", 0, -1, 2**63])
def test_document_expected_version_requires_exact_sqlite_positive_int(tmp_path: Path, bad):
    for mutate in (
        lambda doc: doc.rename("next", expected_version=bad),
        lambda doc: doc.set_acl({}, expected_version=bad),
        lambda doc: doc.save(tmp_path / "document.json", expected_version=bad),
    ):
        doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
        with pytest.raises(ZeError) as exc:
            mutate(doc)
        assert exc.value.code == "validation_failed"
        assert exc.value.context["reason"] == "expected_version_invalid"


def test_document_expected_version_rejects_hostile_equality_without_hook(tmp_path: Path):
    class Hostile:
        calls = 0

        def __eq__(self, other):
            type(self).calls += 1
            raise AssertionError("equality hook must not run")

    bad = Hostile()
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    for mutate in (
        lambda: doc.rename("next", expected_version=bad),
        lambda: doc.set_acl({}, expected_version=bad),
        lambda: doc.save(tmp_path / "document.json", expected_version=bad),
    ):
        with pytest.raises(ZeError) as exc:
            mutate()
        assert exc.value.context["reason"] == "expected_version_invalid"
    assert Hostile.calls == 0


def test_document_save_rejects_version_ceiling_before_memory_or_disk_mutation(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc._v = 2**63 - 1
    before_updated_at = doc.updated_at
    path = tmp_path / "nested" / "document.json"

    with pytest.raises(ZeError) as exc:
        doc.save(path, expected_version=2**63 - 1)
    assert exc.value.code == "version_overflow"
    assert doc.version == 2**63 - 1
    assert doc.updated_at == before_updated_at
    assert not path.parent.exists()


# ---- entity 集成: add / get / find / list / types / hard_remove ----

def _actor():
    return Actor(user_id="u1")


def test_add_entity():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    e = Entity(type="obj", data={"name": "x"})
    ret = doc.add(e)
    assert ret is e
    assert doc.list("obj") == [e]


def test_add_duplicate_id_raises():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    e = Entity(type="obj", data={}, id="dup")
    doc.add(e)
    e2 = Entity(type="obj", data={}, id="dup")
    with pytest.raises(ZeError) as exc:
        doc.add(e2)
    assert exc.value.code == "validation_failed"


def test_add_same_id_across_types_ok():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="a", data={}, id="shared"))
    doc.add(Entity(type="b", data={}, id="shared"))
    assert doc.get("a", "shared").type == "a"
    assert doc.get("b", "shared").type == "b"


def test_get_existing():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    e = Entity(type="obj", data={}, id="e1")
    doc.add(e)
    assert doc.get("obj", "e1") is e


def test_get_missing_raises():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    with pytest.raises(ZeError) as exc:
        doc.get("obj", "nope")
    assert exc.value.code == "entity_not_found"


def test_find_returns_none_when_missing():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    assert doc.find("obj", "nope") is None


def test_find_returns_entity():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    e = Entity(type="obj", data={}, id="e1")
    doc.add(e)
    assert doc.find("obj", "e1") is e


def test_list_filters_deleted_by_default():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    e1 = Entity(type="obj", data={}, id="alive")
    e2 = Entity(type="obj", data={}, id="dead")
    doc.add(e1)
    doc.add(e2)
    e2.delete(expected_version=1, actor=_actor())
    assert doc.list("obj") == [e1]
    assert set(doc.list("obj", include_deleted=True)) == {e1, e2}


def test_list_missing_type_returns_empty():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    assert doc.list("nope") == []


def test_types_sorted():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="zebra", data={}))
    doc.add(Entity(type="alpha", data={}))
    doc.add(Entity(type="mid", data={}))
    assert doc.types() == ["alpha", "mid", "zebra"]


def test_types_excludes_empty_buckets():
    """access doc._collections[type] creates empty bucket; types() shouldn't show it."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    _ = doc.list("never_used")  # touch empty bucket via list
    assert doc.types() == []


def test_hard_remove_physical():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    entity = Entity(type="obj", data={}, id="e1")
    original_schema = entity._schema
    original_validator = entity._validator
    doc.add(entity)
    doc.hard_remove("obj", "e1")
    assert doc.find("obj", "e1") is None
    assert entity._document_owner is None
    assert entity._schema is original_schema
    assert entity._validator is original_validator

    other = Document(schema_version="1.0.0", collaboration_mode="with_save")
    other.add(entity)
    assert other.get("obj", "e1") is entity


def test_hard_remove_idempotent():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.hard_remove("obj", "never-existed")  # no error


def test_hard_remove_read_only():
    """hard_remove 不查 read_only, 真的删."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="obj", data={}, id="ro", read_only=True))
    doc.hard_remove("obj", "ro")
    assert doc.find("obj", "ro") is None


# ---- detect_cycles ----

def test_detect_cycles_no_cycle():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="a", data={"ref": {"$ref": "b/2"}}, id="1"))
    doc.add(Entity(type="b", data={}, id="2"))
    assert doc.detect_cycles() == []


def test_detect_cycles_simple_cycle():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="a", data={"r": {"$ref": "b/2"}}, id="1"))
    doc.add(Entity(type="b", data={"r": {"$ref": "a/1"}}, id="2"))
    cycles = doc.detect_cycles()
    assert len(cycles) == 1
    assert "a/1" in cycles[0] and "b/2" in cycles[0]


def test_detect_cycles_self_loop():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="a", data={"r": {"$ref": "a/1"}}, id="1"))
    cycles = doc.detect_cycles()
    assert len(cycles) == 1


def test_detect_cycles_custom_edge_lookup():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="a", data={"parent_id": "2"}, id="1"))
    doc.add(Entity(type="a", data={"parent_id": "1"}, id="2"))
    def custom_lookup(e):
        pid = e.data.get("parent_id")
        return [("a", pid)] if pid else []
    cycles = doc.detect_cycles(edge_lookup=custom_lookup)
    assert len(cycles) == 1


# ---- cascade_delete ----

def test_cascade_delete_chain():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="a", data={"r": {"$ref": "b/2"}}, id="1"))
    doc.add(Entity(type="b", data={"r": {"$ref": "c/3"}}, id="2"))
    doc.add(Entity(type="c", data={}, id="3"))
    removed = doc.cascade_delete("a", "1", actor=_actor())
    assert ("a", "1") in removed
    assert ("b", "2") in removed
    assert ("c", "3") in removed
    assert doc.find("a", "1").deleted is True
    assert doc.find("b", "2").deleted is True
    assert doc.find("c", "3").deleted is True


def test_cascade_delete_skips_already_deleted():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    e1 = Entity(type="a", data={"r": {"$ref": "b/2"}}, id="1")
    e2 = Entity(type="b", data={}, id="2")
    doc.add(e1)
    doc.add(e2)
    e2.delete(expected_version=1, actor=_actor())
    removed = doc.cascade_delete("a", "1", actor=_actor())
    # e2 已删, 不应再 delete 它
    assert ("b", "2") not in removed


def test_cascade_delete_on_asset_unref_hook():
    """asset_ref 字典 (含 sha256) 应触发 on_asset_unref."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(
        type="obj",
        data={"image": {"sha256": "abc123", "ext": ".png"}},
        id="e1",
    ))
    seen = []
    doc.cascade_delete("obj", "e1", actor=_actor(), on_asset_unref=seen.append)
    assert seen == [{"sha256": "abc123", "ext": ".png"}]


def test_cascade_delete_skips_read_only():
    """cascade_delete 跳过 read_only entity (不软删, 不抛错)."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="a", data={"r": {"$ref": "b/2"}}, id="1"))
    doc.add(Entity(type="b", data={}, id="2", read_only=True))
    removed = doc.cascade_delete("a", "1", actor=_actor())
    assert ("a", "1") in removed
    assert ("b", "2") not in removed
    assert doc.find("b", "2").deleted is False  # 未被软删


# ---- save/open: entity 完整 round-trip ----

def test_save_creates_entities_subdir(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="obj", data={"name": "x"}, id="e1"))
    doc.add(Entity(type="lv", data={"title": "T"}, id="e2"))
    path = tmp_path / "document.json"
    doc.save(path)
    assert _v2_entity_path(path, "obj", "e1").exists()
    assert _v2_entity_path(path, "lv", "e2").exists()


def test_save_writes_collections_index(tmp_path: Path):
    """document.json 的 collections 字段是 {type: [id...]} 索引, 不含 entity 本体."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="obj", data={"name": "x"}, id="e1"))
    path = tmp_path / "document.json"
    doc.save(path)
    raw = json.loads(path.read_text())
    assert raw["collections"] == {"obj": ["e1"]}


def test_save_open_round_trip_with_entities(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="obj", data={"a": 1}, id="e1", name="entity 1"))
    doc.add(Entity(type="obj", data={"a": 2}, id="e2"))
    doc.add(Entity(type="lv", data={"title": "T"}, id="lv1"))
    path = tmp_path / "document.json"
    doc.save(path)

    doc2 = Document.open(path)
    assert doc2.get("obj", "e1").data == {"a": 1}
    assert doc2.get("obj", "e1").name == "entity 1"
    assert doc2.get("obj", "e2").data == {"a": 2}
    assert doc2.get("lv", "lv1").data == {"title": "T"}


def test_save_open_preserves_entity_version_and_log(tmp_path: Path):
    """entity 的 version + actor_log 必须 round-trip 保真."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    e = Entity(type="obj", data={"a": 1}, id="e1")
    doc.add(e)
    e.update({"a": 2}, expected_version=1, actor=_actor())
    e.update({"a": 3}, expected_version=2, actor=_actor())
    # now version=3, actor_log has 2 entries
    path = tmp_path / "document.json"
    doc.save(path)

    doc2 = Document.open(path)
    e_re = doc2.get("obj", "e1")
    assert e_re.version == 3
    assert len(e_re.actor_log()) == 2


def test_save_cleans_orphan_entity_files(tmp_path: Path):
    """hard_remove 后再 save, 旧 entity 文件应被清掉."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="obj", data={}, id="alive"))
    doc.add(Entity(type="obj", data={}, id="orphan"))
    path = tmp_path / "document.json"
    doc.save(path)
    old_orphan_path = _v2_entity_path(path, "obj", "orphan")
    assert old_orphan_path.exists()

    doc.hard_remove("obj", "orphan")
    doc.save(path)
    assert old_orphan_path.exists() is False
    assert _v2_entity_path(path, "obj", "alive").exists()


def test_save_empty_type_dir_cleaned(tmp_path: Path):
    """type 下全部 entity 删完, type 子目录应被清."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="temp", data={}, id="only"))
    path = tmp_path / "document.json"
    doc.save(path)
    old_entity_path = _v2_entity_path(path, "temp", "only")
    assert old_entity_path.exists()

    doc.hard_remove("temp", "only")
    doc.save(path)
    assert old_entity_path.exists() is False
    assert "temp" not in json.loads(path.read_text())["collections"]


def test_open_missing_entity_file_raises(tmp_path: Path):
    """document.json 索引声明的 entity 文件丢了, open 应抛 entity_file_missing."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="obj", data={}, id="e1"))
    path = tmp_path / "document.json"
    doc.save(path)
    # 手动删 entity 文件
    _v2_entity_path(path, "obj", "e1").unlink()
    with pytest.raises(ZeError) as exc:
        Document.open(path)
    assert exc.value.code == "entity_file_missing"


def test_save_crash_writing_index_does_not_brick_project(tmp_path: Path, monkeypatch):
    """崩溃安全回归: save 写 document.json(索引)时崩溃, 项目不能变砖.

    旧序(先删旧实体文件、后写索引)崩在此处会留下"索引引用已删文件" → open() 抛
    entity_file_missing = 项目变砖。新序(先写实体+索引、最后删孤儿)保证任何时刻盘上
    document.json 引用的实体文件都存在, 崩溃后仍能 open。
    """
    path = tmp_path / "document.json"
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="obj", data={}, id="keep"))
    doc.add(Entity(type="obj", data={}, id="drop"))
    doc.save(path)  # v=2; 两实体文件落盘, document.json 引用两者

    # 移除一个实体, 令下一次 save 的"清孤儿"步骤真的有文件要删(才能暴露旧序 bug)
    doc.hard_remove("obj", "drop")

    # 注入崩溃: 新 generation 已完整写入，但 document.json commit point 未发生。
    real_commit = EntityFileStore.commit_document

    def crashing(_store, _payload):
        raise EntityFileStoreError("document_write_failed", "simulated_crash")

    monkeypatch.setattr(EntityFileStore, "commit_document", crashing)
    with pytest.raises(ZeError):
        doc.save(path)
    monkeypatch.setattr(EntityFileStore, "commit_document", real_commit)

    # 关键: 崩溃后项目仍能打开(不变砖)。旧序在此会抛 entity_file_missing。
    reopened = Document.open(path)
    assert reopened.find("obj", "keep") is not None


def test_v2_round_trip_accepts_hostile_looking_utf8_identities(tmp_path: Path):
    path = tmp_path / "document.json"
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    identities = [
        ("../type/../../outside", "/absolute/entity"),
        ("类型/一", "..\\实体/二"),
        ("long", "x" * 4096),
    ]
    for index, (entity_type, entity_id) in enumerate(identities):
        doc.add(Entity(type=entity_type, id=entity_id, data={"index": index}))

    doc.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["entity_storage"] == {
        "schema": "ze.entity-files.v2",
        "layout": "document-version/shard/sha256-v1",
    }
    for index, (entity_type, entity_id) in enumerate(identities):
        stored = _v2_entity_path(path, entity_type, entity_id)
        assert stored.exists()
        assert os.stat(stored).st_mode & 0o077 == 0
        assert Document.open(path).get(entity_type, entity_id).data == {"index": index}
    assert not (tmp_path.parent / "outside").exists()


def test_safe_legacy_0644_tree_opens_then_next_save_migrates_to_v2(tmp_path: Path):
    path = tmp_path / "document.json"
    original = Document(schema_version="1.0.0", collaboration_mode="with_save")
    entity = original.add(Entity(type="card", id="row-1", data={"value": 7}))
    original.save(path)

    legacy_path = tmp_path / "entities" / "card" / "row-1.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(json.dumps(entity.to_dict()), encoding="utf-8")
    legacy_path.chmod(0o644)
    legacy_path.parent.chmod(0o755)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["entity_storage"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    path.chmod(0o644)

    reopened = Document.open(path)
    assert reopened.get("card", "row-1").data == {"value": 7}
    reopened.save(path)

    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["entity_storage"]["schema"] == "ze.entity-files.v2"
    assert _v2_entity_path(path, "card", "row-1").exists()
    assert not legacy_path.exists()
    assert os.stat(path).st_mode & 0o077 == 0


def test_legacy_unicode_space_long_and_dot_ze_identities_migrate_safely(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.json"
    entity_type = ".ze-旧 类型"
    identities = (
        "普通 空格",
        ".ze-历史-id",
        "长名称 " + "x" * 180,
    )
    original = Document(
        id="legacy-unicode",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    for index, entity_id in enumerate(identities):
        original.add(Entity(
            type=entity_type,
            id=entity_id,
            data={"index": index},
        ))
    original.save(path)
    legacy_type = tmp_path / "entities" / entity_type
    legacy_type.mkdir()
    legacy_type.chmod(0o755)
    for entity_id in identities:
        target = legacy_type / (entity_id + ".json")
        target.write_text(
            json.dumps(
                original.get(entity_type, entity_id).to_dict(),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        target.chmod(0o644)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["entity_storage"]
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o644)

    reopened = Document.open(path)
    assert [entity.id for entity in reopened.list(entity_type)] == list(identities)
    reopened.save(path)

    assert not legacy_type.exists()
    for index, entity_id in enumerate(identities):
        assert _v2_entity_path(path, entity_type, entity_id).exists()
        assert Document.open(path).get(entity_type, entity_id).data == {
            "index": index,
        }


def test_group_or_world_writable_document_is_rejected_without_head_switch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.json"
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.save(path)
    before = path.read_bytes()
    before_version = doc.version
    store_root = tmp_path / "entities" / ENTITY_STORE_DIRECTORY
    before_generations = {entry.name for entry in store_root.iterdir()}
    path.chmod(0o666)

    with pytest.raises(ZeError) as opened:
        Document.open(path)
    with pytest.raises(ZeError) as saved:
        doc.save(path)

    assert opened.value.code == "document_storage_path_unsafe"
    assert saved.value.code == "document_storage_path_unsafe"
    assert str(tmp_path) not in str(opened.value)
    assert str(tmp_path) not in str(opened.value.context)
    assert path.read_bytes() == before
    assert doc.version == before_version
    assert {entry.name for entry in store_root.iterdir()} == before_generations


def test_v2_payload_identity_mismatch_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "document.json"
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="card", id="row-1", data={}))
    doc.save(path)
    entity_path = _v2_entity_path(path, "card", "row-1")
    payload = json.loads(entity_path.read_text(encoding="utf-8"))
    payload["id"] = "different"
    entity_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ZeError) as captured:
        Document.open(path)
    assert captured.value.code == "entity_file_identity_mismatch"


def test_v2_read_only_payload_requires_recovery(tmp_path: Path) -> None:
    path = tmp_path / "document.json"
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="card", id="row-1", data={}))
    doc.save(path)
    entity_path = _v2_entity_path(path, "card", "row-1")
    payload = json.loads(entity_path.read_text(encoding="utf-8"))
    payload["read_only"] = True
    entity_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ZeError) as captured:
        Document.open(path)
    assert captured.value.code == "project_recovery_required"


def test_legacy_identity_cannot_escape_project_or_leak_path(tmp_path: Path) -> None:
    path = tmp_path / "document.json"
    outside = tmp_path.parent / "outside.json"
    outside.write_text('{"secret":true}', encoding="utf-8")
    path.write_text(json.dumps({
        "schema": "ze-document",
        "schema_version": "1.0.0",
        "collaboration_mode": "with_save",
        "id": "doc",
        "name": "",
        "collections": {"../outside": ["row"]},
        "_v": 1,
    }), encoding="utf-8")

    with pytest.raises(ZeError) as captured:
        Document.open(path)
    assert captured.value.code == "document_storage_layout_invalid"
    assert str(tmp_path) not in str(captured.value)
    assert outside.read_text(encoding="utf-8") == '{"secret":true}'


def test_digest_collision_has_stable_mapping_error_before_disk_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import document as document_module

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="a", id="one", data={}))
    doc.add(Entity(type="b", id="two", data={}))
    monkeypatch.setattr(document_module, "entity_identity_digest", lambda *_: "0" * 64)

    with pytest.raises(ZeError) as captured:
        doc.save(tmp_path / "document.json")
    assert captured.value.code == "document_storage_mapping_collision"
    assert not (tmp_path / "document.json").exists()


def test_post_rename_fsync_unknown_keeps_memory_and_visible_version_aligned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import entity_file_store as store_module

    path = tmp_path / "document.json"
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    before = doc.version
    real_fsync_directory = store_module._fsync_directory

    def fail_only_after_commit(descriptor: int, *, before_commit: bool) -> None:
        if not before_commit:
            raise EntityFileStoreError(
                "document_durability_unknown",
                "simulated_root_fsync_failure",
                committed=True,
            )
        real_fsync_directory(descriptor, before_commit=True)

    monkeypatch.setattr(store_module, "_fsync_directory", fail_only_after_commit)
    with pytest.raises(ZeError) as captured:
        doc.save(path)

    assert captured.value.code == "document_durability_unknown"
    assert captured.value.context["commit_point_reached"] is True
    assert doc.version == before + 1
    assert json.loads(path.read_text(encoding="utf-8"))["_v"] == doc.version


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":"1","collaboration_mode":"with_save",'
        '"id":"d","name":"","collections":{},"n":' + "9" * 4301 + "}",
        '{"schema_version":"1","schema_version":"2",'
        '"collaboration_mode":"with_save","id":"d","name":"",'
        '"collections":{}}',
        '{"schema_version":"1","collaboration_mode":"with_save",'
        '"id":"d","name":"","collections":{},"deep":' + "[" * 121 + "0" + "]" * 121 + "}",
    ],
)
def test_open_resource_and_duplicate_key_failures_are_stable_recovery(
    tmp_path: Path,
    payload: str,
) -> None:
    path = tmp_path / "document.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ZeError) as captured:
        Document.open(path)
    assert captured.value.code == "project_recovery_required"


# ---- DocumentRegistry 完整流转 (declare → build → bootstrap → save → open) ----

def test_registry_collects_declarations():
    r = DocumentRegistry()
    r.declare_entity_type(
        name="object",
        schema={"name": {"type": "string"}},
        collaboration_unit=True,
    )
    r.set_collaboration_mode("live_collab")
    r.declare_read_only_source(name="ext", entity_type="level", loader_path="x.json")
    r.set_demo_project(name="demo", bootstrap=lambda doc: None)

    assert r.entity_types == {"object": {
        "schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        },
        "collaboration_unit": True,
    }}
    assert r.collaboration_mode == "live_collab"
    assert r.read_only_sources == [
        {"name": "ext", "entity_type": "level", "loader_path": "x.json"}
    ]
    assert r.demo is not None
    assert r.demo["name"] == "demo"


def test_registry_duplicate_entity_type_raises():
    r = DocumentRegistry()
    r.declare_entity_type(name="object", schema={})
    with pytest.raises(ZeError) as exc:
        r.declare_entity_type(name="object", schema={})
    assert exc.value.code == "validation_failed"


@pytest.mark.parametrize("bad", [[], 0, "", None])
def test_registry_rejects_falsey_non_object_entity_schema(bad):
    registry = DocumentRegistry()
    with pytest.raises(ZeError) as exc:
        registry.declare_entity_type(name="card", schema=bad)
    assert exc.value.code == "validation_failed"
    assert registry.entity_types == {}


@pytest.mark.parametrize("name", ["", "__document__", "\ud800"])
def test_registry_rejects_invalid_entity_type_name(name):
    registry = DocumentRegistry()
    with pytest.raises(ZeError) as exc:
        registry.declare_entity_type(name=name, schema={})
    assert exc.value.code == "validation_failed"
    assert registry.entity_types == {}


def test_registry_duplicate_read_only_source_raises():
    r = DocumentRegistry()
    r.declare_read_only_source(name="src", entity_type="t", loader_path="x")
    with pytest.raises(ZeError) as exc:
        r.declare_read_only_source(name="src", entity_type="t", loader_path="x")
    assert exc.value.code == "validation_failed"


def test_registry_invalid_collaboration_mode_raises():
    r = DocumentRegistry()
    with pytest.raises(ZeError) as exc:
        r.set_collaboration_mode("nope")
    assert exc.value.code == "validation_failed"


def test_registry_invalid_save_policy_raises():
    r = DocumentRegistry()
    with pytest.raises(ZeError) as exc:
        r.set_save_policy("manual")
    assert exc.value.code == "validation_failed"


def test_registry_accepts_realtime_checkpoint():
    r = DocumentRegistry()
    r.set_collaboration_mode("realtime_checkpoint")
    assert r.collaboration_mode == "realtime_checkpoint"


def test_registry_build_document():
    r = DocumentRegistry()
    r.declare_entity_type(name="object", schema={"name": {"type": "string"}})
    r.set_collaboration_mode("with_save")
    doc = r.build_document(schema_version="1.0.0", name="proj")
    assert doc.name == "proj"
    assert doc.collaboration_mode == "with_save"
    assert doc.schema == {"object": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }}


def test_registry_build_document_passes_save_policy():
    r = DocumentRegistry()
    r.declare_entity_type(name="object", schema={})
    r.set_collaboration_mode("live_collab")
    r.set_save_policy("auto_commit")

    assert r.save_policy == "auto_commit"
    doc = r.build_document(schema_version="1.0.0")
    assert doc.save_policy == "auto_commit"
    assert doc.effective_save_policy == "auto_commit"
    assert doc.should_auto_commit is True


def test_registry_without_save_policy_keeps_legacy_declaration_absent():
    r = DocumentRegistry()
    r.declare_entity_type(name="object", schema={})
    r.set_collaboration_mode("with_save")

    assert r.save_policy is None
    doc = r.build_document(schema_version="1.0.0")
    assert doc.save_policy is None
    assert doc.effective_save_policy == "auto_commit"


def test_registry_build_document_requires_collaboration_mode():
    r = DocumentRegistry()
    r.declare_entity_type(name="object", schema={})
    with pytest.raises(ZeError) as exc:
        r.build_document(schema_version="1.0.0")
    assert exc.value.code == "validation_failed"
    assert "collaboration_mode" in str(exc.value)


def test_registry_build_document_requires_entity_type():
    r = DocumentRegistry()
    r.set_collaboration_mode("with_save")
    with pytest.raises(ZeError) as exc:
        r.build_document(schema_version="1.0.0")
    assert exc.value.code == "validation_failed"
    assert "entity_type" in str(exc.value)


def test_registry_entity_types_returns_copy():
    r = DocumentRegistry()
    r.declare_entity_type(name="object", schema={"a": {"type": "integer"}})
    types = r.entity_types
    types["fake"] = {}
    assert "fake" not in r.entity_types


def test_registry_read_only_sources_returns_copy():
    r = DocumentRegistry()
    r.declare_read_only_source(name="s1", entity_type="t", loader_path="x")
    srcs = r.read_only_sources
    srcs.append({"name": "fake", "entity_type": "t", "loader_path": "y"})
    assert len(r.read_only_sources) == 1


def test_registry_open_migrations_default_empty():
    assert DocumentRegistry().open_migrations == []


def test_registry_declare_open_migration_accumulates_in_order():
    r = DocumentRegistry()
    a, b = (lambda doc: False), (lambda doc: True)
    r.declare_open_migration(a)
    r.declare_open_migration(b)
    assert r.open_migrations == [a, b]          # 声明顺序即调用顺序


def test_registry_open_migrations_returns_copy():
    r = DocumentRegistry()
    r.declare_open_migration(lambda doc: False)
    got = r.open_migrations
    got.append(lambda doc: True)
    assert len(r.open_migrations) == 1          # 访问器返副本, 外部改动不回灌


def test_registry_full_flow_with_bootstrap_and_persistence(tmp_path: Path):
    """完整流转: declare → set_mode → set_demo → build → bootstrap → save → open."""
    registry = DocumentRegistry()
    registry.declare_entity_type(
        name="object",
        schema={"name": {"type": "string"}},
    )
    registry.set_collaboration_mode("with_save")
    registry.set_demo_project(
        name="demo",
        bootstrap=lambda doc: [
            doc.add(Entity(type="object", data={"name": "sample-1"})),
            doc.add(Entity(type="object", data={"name": "sample-2"})),
        ],
    )
    doc = registry.build_document(schema_version="1.0.0", name="proj")
    # bootstrap
    registry.demo["bootstrap"](doc)
    assert len(doc.list("object")) == 2

    # 持久化 round-trip
    path = tmp_path / "document.json"
    doc.save(path)
    reopened = Document.open(path)
    assert len(reopened.list("object")) == 2
    names = sorted(e.get("name") for e in reopened.list("object"))
    assert names == ["sample-1", "sample-2"]


# ==============================================================
# doc.add 自动注入 field-types validator (04-field-types 集成)
# ==============================================================

def test_doc_add_auto_injects_validator():
    """doc.schema 含 entity_type 的 schema 时, doc.add(entity) 自动注入 validator."""
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={
            "obj": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "required": True},
                },
            },
        },
    )
    e = Entity(type="obj", data={"name": "x"})
    assert not e.has_validator        # add 前没 validator
    doc.add(e)
    assert e.has_validator             # add 后注入了


def test_doc_add_rejects_invalid_data():
    """add 时 entity.data 不符合 schema 应抛 schema_violation."""
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={
            "obj": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "required": True},
                },
            },
        },
    )
    e = Entity(type="obj", data={})       # 缺 required: name
    with pytest.raises(ZeError) as exc:
        doc.add(e)
    assert exc.value.code == "schema_violation"


def test_doc_add_skips_injection_when_no_schema_for_type():
    """doc.schema 没该 entity.type 的 schema 时, 不注入 (允许加任意 entity)."""
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"obj": {"type": "object"}},     # 没 "other"
    )
    e = Entity(type="other", data={"anything": 1})
    doc.add(e)
    assert not e.has_validator


def test_doc_add_skips_injection_when_doc_schema_empty():
    """doc.schema 是空 dict 时不注入."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    e = Entity(type="obj", data={"x": 1})
    doc.add(e)
    assert not e.has_validator


def test_doc_add_overrides_existing_custom_validator_with_document_authority():
    """standalone custom validator 不能跟随 Entity 进入 Document。"""
    called = []
    custom = lambda d, s, p: called.append("custom")
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"obj": {"type": "object", "properties": {
            "name": {"type": "string"}}}},
    )
    e = Entity(type="obj", data={"name": "x"},
               schema={"some": "schema"},   # 构造时 schema 非空
               validator=custom)
    assert called == ["custom"]              # 构造时跑了一次
    called.clear()
    doc.add(e)
    assert is_framework_validator(e._validator)
    assert e._validator is not custom
    assert called == []

    before = e.to_dict()
    with pytest.raises(ZeError) as exc:
        e.update(
            {"name": 42},
            expected_version=1,
            actor=Actor(user_id="u"),
            validator=lambda data, schema, path: None,
        )
    assert exc.value.code == "validation_failed"
    assert e.to_dict() == before

    with pytest.raises(ZeError) as exc:
        e.update(
            {"name": 42},
            expected_version=1,
            actor=Actor(user_id="u"),
        )
    assert exc.value.code == "schema_violation"
    assert called == []


def test_doc_add_custom_validator_cannot_admit_data_rejected_by_document_schema():
    custom = lambda data, schema, path: None
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"obj": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }},
    )
    entity = Entity(
        type="obj",
        data={"name": 42},
        schema={"anything": {}},
        validator=custom,
    )

    with pytest.raises(ZeError) as exc:
        doc.add(entity)

    assert exc.value.code == "schema_violation"
    assert doc.find("obj", entity.id) is None
    assert entity._validator is custom


@pytest.mark.parametrize("document_schema", [{}, {"obj": {}}])
def test_doc_add_empty_or_missing_type_schema_clears_custom_validator(document_schema):
    custom = lambda data, schema, path: None
    entity = Entity(
        type="obj",
        data={"x": 1},
        schema={"x": {"type": "integer"}},
        validator=custom,
    )
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema=document_schema,
    )

    doc.add(entity)

    assert entity._schema == {}
    assert entity._validator is _no_op_validator
    assert entity.has_validator is False
    assert entity._document_owner is doc


def test_document_owner_binding_blocks_public_validator_replacement_and_cross_document():
    first = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"obj": {"value": {"type": "integer"}}},
    )
    second = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"obj": {"value": {"type": "integer"}}},
    )
    entity = Entity(type="obj", id="e", data={"value": 1})
    first.add(entity)
    original_schema = entity._schema
    original_validator = entity._validator

    for method in (entity.install_validator, entity.attach_validator):
        with pytest.raises(ZeError) as exc:
            method({}, _no_op_validator)
        assert exc.value.code == "validation_failed"
        assert exc.value.context["reason"] == "document_validator_authority_owned"
        assert entity._schema is original_schema
        assert entity._validator is original_validator
        assert entity._document_owner is first

    with pytest.raises(ZeError) as exc:
        second.add(entity)
    assert exc.value.code == "validation_failed"
    assert exc.value.context["reason"] == "entity_document_owner_mismatch"
    assert second.types() == []
    assert first.get("obj", "e") is entity
    assert entity._document_owner is first


def test_same_document_can_privately_rebind_authority_for_future_schema_migration():
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"obj": {"value": {"type": "integer"}}},
    )
    entity = doc.add(Entity(type="obj", data={"value": 1}))
    original_validator = entity._validator

    doc._apply_validator_authority(entity, validate_current=True)

    assert entity._document_owner is doc
    assert entity._validator is not original_validator
    assert is_framework_validator(entity._validator, doc.schema["obj"])


def test_doc_add_field_type_integration_full():
    """完整集成: 业务方声明 schema → doc.add 自动校验 → entity.update 也校验."""
    actor = Actor(user_id="u1")
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={
            "entry": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "required": True,
                              "minLength": 1, "maxLength": 200},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    )
    e = Entity(type="entry", data={"title": "Hello", "tags": ["a", "b"]})
    doc.add(e)
    assert e.has_validator
    # update 通过
    e.update({"title": "World"}, expected_version=1, actor=actor)
    # update 对完整候选 data 跑同一 authority，空 string 被拒。
    with pytest.raises(ZeError) as exc:
        e.update({"title": ""}, expected_version=e.version, actor=actor)
    assert exc.value.code == "schema_violation"


def test_legacy_flat_schema_is_normalized_for_constructor_and_update():
    legacy = {
        "title": {"type": "string", "required": True},
        "count": {"type": "integer"},
    }
    validator = make_validator(legacy)

    with pytest.raises(ZeError) as exc:
        Entity(type="entry", data={"count": 1}, schema=legacy, validator=validator)
    assert exc.value.code == "schema_violation"

    entity = Entity(
        type="entry",
        data={"title": "ok", "count": 1},
        schema=legacy,
        validator=validator,
    )
    with pytest.raises(ZeError) as exc:
        entity.update(
            {"count": "bad"},
            expected_version=1,
            actor=Actor(user_id="u"),
        )
    assert exc.value.code == "schema_violation"
    assert entity.version == 1
    assert entity.data == {"title": "ok", "count": 1}


def test_doc_update_complete_candidate_enforces_additional_properties_false():
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"entry": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        }},
    )
    entity = doc.add(Entity(type="entry", data={"title": "ok"}))

    with pytest.raises(ZeError) as exc:
        entity.update(
            {"unexpected": True},
            expected_version=1,
            actor=Actor(user_id="u"),
        )

    assert exc.value.code == "schema_violation"
    assert entity.version == 1
    assert entity.data == {"title": "ok"}


def test_internal_add_attaches_document_authority_without_revalidating_history():
    calls = []
    custom = lambda data, schema, path: calls.append(path)
    dirty = Entity(
        type="entry",
        data={"count": 1},
        schema={"custom": {}},
        validator=custom,
    )
    calls.clear()
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"entry": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["title"],
        }},
    )

    doc._add_entity_internal(dirty)

    assert calls == []
    assert is_framework_validator(dirty._validator)
    with pytest.raises(ZeError) as exc:
        dirty.update(
            {"count": 2},
            expected_version=1,
            actor=Actor(user_id="u"),
        )
    assert exc.value.code == "schema_violation"
    assert dirty.version == 1
    assert dirty.data == {"count": 1}


def test_doc_update_complete_candidate_enforces_root_one_of_across_fields():
    schema = {
        "oneOf": [
            {
                "type": "object",
                "properties": {"left": {"type": "string"}},
                "required": ["left"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"right": {"type": "integer"}},
                "required": ["right"],
                "additionalProperties": False,
            },
        ],
    }
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"entry": schema},
    )
    entity = doc.add(Entity(type="entry", data={"left": "ok"}))

    with pytest.raises(ZeError) as exc:
        entity.update(
            {"right": 1},
            expected_version=1,
            actor=Actor(user_id="u"),
        )

    assert exc.value.code == "schema_violation"
    assert entity.version == 1
    assert entity.data == {"left": "ok"}


def test_document_rejects_entity_subclasses_before_accessing_properties():
    class EntitySubclass(Entity):
        @property
        def type(self):
            raise AssertionError("subclass property must not run")

    entity = EntitySubclass(type="entry", data={})
    for method_name in ("add", "_add_entity_internal"):
        doc = Document(
            schema_version="1.0.0",
            collaboration_mode="with_save",
        )
        with pytest.raises(ZeError) as exc:
            getattr(doc, method_name)(entity)
        assert exc.value.code == "validation_failed"
        assert exc.value.context["reason"] == "entity_instance_invalid"
        assert doc.types() == []


def test_read_only_source_materialization_injects_document_validator():
    """register_read_only_source 物化路径也应自动注入 Document 权威校验器."""
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={
            "level": {
                "type": "object",
                "properties": {"title": {"type": "string", "required": True}},
            },
        },
    )
    # loader 提供合法 entity data
    doc.register_read_only_source(ReadOnlySourceSpec(
        name="lvls",
        entity_type="level",
        loader=lambda: [{"id": "lvl1", "data": {"title": "L1"}}],
    ))
    e = doc.get("level", "lvl1")
    assert e.has_validator
    assert e.read_only is True


# ---- Document.open 恢复真校验器 (attach_validator; entity_ref 裸字符串根因回归) ----

_REF_SCHEMA = {
    "proj": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "required": True},
            # entity_ref 需 {"$ref": "type/id"} dict; 裸字符串应被拒
            "active_plugin": {"type": "entity_ref"},
        },
    },
}


def _saved_ref_doc(tmp_path: Path, active_plugin: object) -> Path:
    """建一个含 proj 实体的文档并落盘; active_plugin 由调用方给 (合法/非法皆可)."""
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save", schema=_REF_SCHEMA)
    e = Entity(type="proj", data={"title": "P", "active_plugin": active_plugin})
    doc.add(e)
    path = tmp_path / "document.json"
    doc.save(path)
    return path


def test_open_installs_validator_on_loaded_entities(tmp_path: Path):
    """Document.open 后, 加载态实体应带真校验器 (此前直插 bucket 绕过注入 → 恒 no-op)."""
    path = _saved_ref_doc(tmp_path, {"$ref": "plugin/p1"})
    reopened = Document.open(path)
    e = reopened.list("proj")[0]
    assert e.has_validator


def test_open_loaded_entity_update_rejects_schema_violation(tmp_path: Path):
    """真回归: 重新打开的实体 update() 把裸字符串写进 entity_ref 字段应被拒 (报告的 bug)."""
    actor = Actor(user_id="u1")
    path = _saved_ref_doc(tmp_path, {"$ref": "plugin/p1"})
    reopened = Document.open(path)
    e = reopened.list("proj")[0]
    with pytest.raises(ZeError) as exc:
        e.update({"active_plugin": '{"$ref":"plugin/p2"}'}, expected_version=e.version, actor=actor)
    assert exc.value.code == "schema_violation"
    # 合法 dict 仍通过
    e.update({"active_plugin": {"$ref": "plugin/p2"}}, expected_version=e.version, actor=actor)
    assert e.data["active_plugin"] == {"$ref": "plugin/p2"}


def test_open_tolerates_preexisting_corrupt_data(tmp_path: Path):
    """attach 不立即重校: 落盘时已是脏值 (entity_ref 裸字符串) 的文档仍能被 open, 不抛."""
    # 构造脏文档: active_plugin 是裸字符串 (违反 entity_ref), 绕过 add 的即时校验落盘
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save", schema=_REF_SCHEMA)
    # 用 from_dict 造脏实体 (无 validator, 不校验), 直插 bucket 再落盘 —— 复现历史脏数据
    dirty = Entity.from_dict(
        {"id": "d1", "type": "proj", "version": 3,
         "data": {"title": "P", "active_plugin": "plugin/p1"}},   # 裸字符串
    )
    doc._collections["proj"]["d1"] = dirty
    path = tmp_path / "document.json"
    doc.save(path)
    # open 不应抛 (attach_validator 不重校历史 data)
    reopened = Document.open(path)
    e = reopened.get("proj", "d1")
    assert e.has_validator                       # 校验器已挂 (未来 update 生效)
    assert e.data["active_plugin"] == "plugin/p1"  # 历史脏值原样保留 (读容忍)


def test_apply_patch_directly_replays_dirty_value_without_revalidation(tmp_path: Path):
    """回归: undo/redo/sync 重放 reverse-patch 恢复历史脏值时不重校.

    attach_validator (1.105.0) 给加载态实体挂真校验器后, 若历史脏值 (entity_ref 字段
    裸字符串) 被前向编辑改成合法 $ref, 撤销该编辑 = apply_patch_directly 重放一条把字段
    设回裸字符串的 reverse-patch。重放必须忠实还原早前状态、不能被真 validator 拒收
    (否则 undo 报 schema_violation 中止 —— 修复前的 bug)。前向编辑仍走真 validator。
    """
    from operation import Patch

    actor = Actor(user_id="u1")
    # 历史脏文档: active_plugin 是裸字符串 (违反 entity_ref schema), 绕过即时校验落盘
    path = tmp_path / "document.json"
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save", schema=_REF_SCHEMA)
    dirty = Entity.from_dict(
        {"id": "d1", "type": "proj", "version": 3,
         "data": {"title": "P", "active_plugin": "plugin/p1"}},   # 裸字符串
    )
    doc._collections["proj"]["d1"] = dirty
    doc.save(path)

    reopened = Document.open(path)                       # 挂真 validator
    e = reopened.get("proj", "d1")
    assert e.has_validator

    # 前向编辑: 改成合法 $ref (走真 validator, 通过)
    e.update({"active_plugin": {"$ref": "plugin/p2"}},
             expected_version=e.version, actor=actor)
    assert e.data["active_plugin"] == {"$ref": "plugin/p2"}

    # 撤销重放: 把字段设回历史裸字符串 (reverse-patch)。修复前这里 schema_violation 中止。
    reverse = Patch(entity_type="proj", entity_id="d1", field="active_plugin",
                    op="set", before={"$ref": "plugin/p2"}, after="plugin/p1")
    reopened.apply_patch_directly(
        reverse, actor=actor,
        _replay_capability=_HISTORY_REPLAY_CAPABILITY,
    )  # 不应抛
    assert reopened.get("proj", "d1").data["active_plugin"] == "plugin/p1"


# ---- A3: apply_patches_atomically(全有或全无) ----

def test_apply_patches_atomically_success_applies_all():
    """成功路径:一组 patch 全部按序应用。"""
    from operation import Patch
    actor = Actor(user_id="u1")
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="t", id="a", data={"x": 1}))
    doc.add(Entity(type="t", id="b", data={"x": 2}))
    doc.apply_patches_atomically([
        Patch(entity_type="t", entity_id="a", field="x", op="set", before=1, after=11),
        Patch(entity_type="t", entity_id="b", field="x", op="set", before=2, after=22),
    ], actor=actor, _replay_capability=_HISTORY_REPLAY_CAPABILITY)
    assert doc.get("t", "a").data["x"] == 11
    assert doc.get("t", "b").data["x"] == 22


def test_apply_patches_atomically_rolls_back_field_set_on_midway_failure():
    """A3 回归:一组 patch 中途失败(命中不存在的 entity)→ 已应用的前序 patch 精确回滚、doc 零变更。

    旧的逐条 apply 循环(undo/redo/rollback)会留下"半套用"内存态,下条命令 save 持久化 → doc 与 oplog 分叉。"""
    from operation import Patch
    actor = Actor(user_id="u1")
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="t", id="a", data={"x": 1}))
    original = doc.get("t", "a")
    p_ok = Patch(entity_type="t", entity_id="a", field="x", op="set", before=1, after=99)
    p_bad = Patch(entity_type="t", entity_id="missing", field="x", op="set", before=None, after=7)
    with pytest.raises(ZeError) as ei:
        doc.apply_patches_atomically(
            [p_ok, p_bad], actor=actor,
            _replay_capability=_HISTORY_REPLAY_CAPABILITY,
        )
    assert ei.value.code == "entity_not_found"
    assert doc.get("t", "a").data["x"] == 1          # p_ok 被回滚,未留 99
    assert doc.get("t", "a").version == 1            # 版本也精确还原(未 bump)
    assert doc.get("t", "a") is original              # 外部引用不因回滚失效


def test_apply_patches_atomically_removes_created_entity_on_failure():
    """A3 回归:先 create 一个 entity、后一条 patch 失败 → 新建的 entity 在回滚中被移除(调用前不存在)。"""
    from operation import Patch, ENTITY_META_FIELD
    actor = Actor(user_id="u1")
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    created = Entity(type="t", id="new", data={"y": 5}).to_dict()
    p_create = Patch(entity_type="t", entity_id="new", field=ENTITY_META_FIELD,
                     op="create", before=None, after=created)
    p_bad = Patch(entity_type="t", entity_id="missing", field="x", op="set", before=None, after=1)
    with pytest.raises(ZeError):
        doc.apply_patches_atomically(
            [p_create, p_bad], actor=actor,
            _replay_capability=_HISTORY_REPLAY_CAPABILITY,
        )
    assert doc.find("t", "new") is None               # 新建的被移除,调用前状态精确还原


def test_replay_create_restores_historical_dirty_data_then_enforces_future_schema():
    from operation import ENTITY_META_FIELD, Patch

    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"card": {"title": {"type": "string"}}},
    )
    historical = Entity(
        type="card", id="dirty", data={"title": 123}
    ).to_dict()
    create = Patch(
        entity_type="card",
        entity_id="dirty",
        field=ENTITY_META_FIELD,
        op="create",
        before=None,
        after=historical,
    )

    doc.apply_patch_directly(
        create,
        actor=Actor(user_id="undo"),
        _replay_capability=_HISTORY_REPLAY_CAPABILITY,
    )

    restored = doc.get("card", "dirty")
    assert restored.data == {"title": 123}
    assert restored._document_owner is doc
    assert is_framework_validator(restored._validator, doc.schema["card"])
    with pytest.raises(ZeError) as exc:
        restored.update(
            {"title": 456},
            expected_version=restored.version,
            actor=Actor(user_id="u"),
        )
    assert exc.value.code == "schema_violation"


def test_guarded_history_replay_rejects_obsolete_value_and_rolls_back_exactly():
    from operation import Patch

    doc = Document(
        schema_version="2.0.0",
        collaboration_mode="live_collab",
        schema={"clip": {"track": {
            "type": "integer", "minimum": 0, "maximum": 63,
            HISTORY_REPLAY_SCHEMA_GUARD: True,
        }}},
    )
    entity = doc.add(Entity(type="clip", id="clip-1", data={"track": 3}))
    before = entity.to_dict()
    resurrect = Patch(
        entity_type="clip", entity_id="clip-1", field="track",
        op="set", before=3, after=64,
    )

    with pytest.raises(ZeError) as error:
        doc.apply_patch_directly(
            resurrect,
            actor=Actor(user_id="redo"),
            _replay_capability=_HISTORY_REPLAY_CAPABILITY,
        )

    assert error.value.code == "schema_violation"
    assert doc.get("clip", "clip-1") is entity
    assert entity.to_dict() == before


def test_guarded_history_replay_validates_the_final_batch_not_intermediate_values():
    from operation import Patch

    doc = Document(
        schema_version="2.0.0",
        collaboration_mode="live_collab",
        schema={"clip": {"track": {
            "type": "integer", "minimum": 0, "maximum": 63,
            HISTORY_REPLAY_SCHEMA_GUARD: True,
        }}},
    )
    entity = doc.add(Entity(type="clip", id="clip-1", data={"track": 3}))
    doc.apply_patches_atomically(
        [
            Patch(entity_type="clip", entity_id="clip-1", field="track",
                  op="set", before=3, after=64),
            Patch(entity_type="clip", entity_id="clip-1", field="track",
                  op="set", before=64, after=63),
        ],
        actor=Actor(user_id="redo"),
        _replay_capability=_HISTORY_REPLAY_CAPABILITY,
    )

    assert entity.data["track"] == 63


def test_guarded_history_replay_rejects_obsolete_entity_create_and_removes_it():
    from operation import Patch

    doc = Document(
        schema_version="2.0.0",
        collaboration_mode="live_collab",
        schema={"clip": {"track": {
            "type": "integer", "minimum": 0, "maximum": 63,
            HISTORY_REPLAY_SCHEMA_GUARD: True,
        }}},
    )
    historical = Entity(type="clip", id="legacy", data={"track": 64}).to_dict()
    create = Patch(
        entity_type="clip", entity_id="legacy", field="__entity__",
        op="create", before=None, after=historical,
    )

    with pytest.raises(ZeError) as error:
        doc.apply_patch_directly(
            create,
            actor=Actor(user_id="redo"),
            _replay_capability=_HISTORY_REPLAY_CAPABILITY,
        )

    assert error.value.code == "schema_violation"
    assert doc.find("clip", "legacy") is None


def test_guarded_history_replay_does_not_validate_unmarked_legacy_fields():
    from operation import Patch

    doc = Document(
        schema_version="2.0.0",
        collaboration_mode="live_collab",
        schema={"clip": {
            "track": {
                "type": "integer", "minimum": 0, "maximum": 63,
                HISTORY_REPLAY_SCHEMA_GUARD: True,
            },
            "legacy_title": {"type": "string"},
        }},
    )
    historical = Entity(type="clip", id="legacy", data={
        "track": 3, "legacy_title": 123,
    }).to_dict()
    doc.apply_patch_directly(
        Patch(
            entity_type="clip", entity_id="legacy", field="__entity__",
            op="create", before=None, after=historical,
        ),
        actor=Actor(user_id="redo"),
        _replay_capability=_HISTORY_REPLAY_CAPABILITY,
    )
    doc.apply_patch_directly(
        Patch(
            entity_type="clip", entity_id="legacy", field="legacy_title",
            op="set", before=123, after=456,
        ),
        actor=Actor(user_id="redo"),
        _replay_capability=_HISTORY_REPLAY_CAPABILITY,
    )

    assert doc.get("clip", "legacy").data == {
        "track": 3, "legacy_title": 456,
    }


def test_history_replay_entrypoints_require_exact_private_capability():
    from operation import Patch

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="t", id="e", data={"x": 1}))
    patch = Patch("t", "e", "x", "set", 1, 2)
    actor = Actor(user_id="u")

    for call in (
        lambda: doc.apply_patch_directly(patch, actor=actor),
        lambda: doc.apply_patch_directly(
            patch, actor=actor, _replay_capability=object(),
        ),
        lambda: doc.apply_patches_atomically([patch], actor=actor),
        lambda: doc.apply_patches_atomically(
            [patch], actor=actor, _replay_capability=object(),
        ),
    ):
        with pytest.raises(ZeError) as exc:
            call()
        assert exc.value.code == "history_replay_forbidden"
    assert doc.get("t", "e").data == {"x": 1}


def test_history_replay_rejects_patch_subclasses_and_hostile_fields_without_hooks():
    from operation import Patch

    class DerivedPatch(Patch):
        pass

    class Hostile:
        calls = 0

        def __eq__(self, other):
            type(self).calls += 1
            raise AssertionError("equality hook must not run")

        def __str__(self):
            type(self).calls += 1
            raise AssertionError("string hook must not run")

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    actor = Actor(user_id="u")
    derived = DerivedPatch("t", "e", "x", "set", 1, 2)
    with pytest.raises(TypeError, match="exact string"):
        Patch(Hostile(), "e", "x", "set", 1, 2)

    for patch in (derived,):
        with pytest.raises(ZeError) as exc:
            doc.apply_patch_directly(
                patch, actor=actor,
                _replay_capability=_HISTORY_REPLAY_CAPABILITY,
            )
        assert exc.value.context["reason"] == "history_replay_patch_invalid"
    with pytest.raises(ZeError) as exc:
        doc.apply_patches_atomically(
            [derived], actor=actor,
            _replay_capability=_HISTORY_REPLAY_CAPABILITY,
        )
    assert exc.value.context["reason"] == "history_replay_patch_invalid"
    assert Hostile.calls == 0


@pytest.mark.parametrize(("payload_type", "payload_id"), [("other", "e"), ("t", "other")])
def test_replay_create_requires_patch_and_payload_identity_match(payload_type, payload_id):
    from operation import ENTITY_META_FIELD, Patch

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    payload = Entity(type=payload_type, id=payload_id, data={}).to_dict()
    patch = Patch("t", "e", ENTITY_META_FIELD, "create", None, payload)

    with pytest.raises(ZeError) as exc:
        doc.apply_patch_directly(
            patch,
            actor=Actor(user_id="undo"),
            _replay_capability=_HISTORY_REPLAY_CAPABILITY,
        )
    assert exc.value.context["reason"] == "history_replay_create_identity_mismatch"
    assert doc.types() == []


# ---- W04: full mutation snapshot / restore / one-shot session ----

def test_mutation_snapshot_restores_all_document_metadata_and_container_identity():
    loader = lambda: []
    doc = Document(
        schema_version="2.3.4",
        collaboration_mode="live_collab",
        save_policy="checkpoint_commit",
        name="before",
        id="doc-snapshot",
        schema={"t": {"properties": {"x": {"type": "number"}}}},
        config={"canvas": {"width": 100}},
        acl={"owner": "u1", "writers": ["u2"]},
        harvest_rules=[{"when_role": "ze.field.x@1"}],
    )
    doc.register_read_only_source(ReadOnlySourceSpec(
        name="catalog", entity_type="t", loader=loader,
    ))
    empty_bucket = doc._collections["empty"]
    collections = doc._collections
    before_metadata = copy.deepcopy({
        key: value for key, value in doc.__dict__.items()
        if key != "_collections"
    })
    snapshot = doc.capture_mutation_snapshot()

    assert isinstance(snapshot, DocumentMutationSnapshot)
    assert snapshot.document_id == "doc-snapshot"

    # 模拟 handler/extension 修改所有类别的 Document 内存字段，甚至替换容器。
    doc._id = "mutated-id"
    doc._name = "after"
    doc._schema_version = "99.0.0"
    doc._collaboration_mode = "with_save"
    doc._save_policy = "auto_commit"
    doc._schema = {"other": {}}
    doc._config = {"canvas": {"width": 999}}
    doc._acl = {"owner": "intruder"}
    doc._harvest_rules = []
    doc._read_only_sources = {}
    doc._v = 888
    doc._created_at = -1.0
    doc._updated_at = -2.0
    doc._collections = {}
    doc._session_only_field = "must disappear"

    same_document = doc
    doc.restore_mutation_snapshot(snapshot)

    assert doc is same_document
    assert {
        key: value for key, value in doc.__dict__.items()
        if key != "_collections"
    } == before_metadata
    assert not hasattr(doc, "_session_only_field")
    assert doc._collections is collections
    assert doc._collections["empty"] is empty_bucket
    assert list(doc._collections) == ["empty"]


def test_mutation_session_restores_exact_entity_dict_and_external_reference():
    actor = Actor(user_id="u1", device_id="d1")
    provenance = AiProvenance(actor="u1", model="m1", trace_id="trace-before")

    def validator(data, schema, path):
        return None

    entity = Entity(
        type="t",
        id="e1",
        name="original",
        data={"x": 1, "nested": {"a": [1, 2]}},
        schema={"properties": {"x": {"type": "number"}}},
        validator=validator,
    )
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(entity)
    entity.update(
        {"x": 2},
        expected_version=entity.version,
        actor=actor,
        ai_provenance={"x": provenance},
    )
    before = copy.deepcopy({
        key: value
        for key, value in entity.__dict__.items()
        if key not in ("_validator", "_document_owner")
    })
    original_validator = entity._validator
    original_owner = entity._document_owner
    external_reference = entity
    session = doc.begin_mutation_session()

    entity.update(
        {"x": 9, "nested": {"a": [99]}},
        name="mutated",
        expected_version=entity.version,
        actor=actor,
        ai_provenance={
            "x": AiProvenance(actor="u1", model="m2", trace_id="trace-after"),
        },
    )
    entity.delete(expected_version=entity.version, actor=actor)
    entity._validator = lambda *_: None
    entity._created_at = 0.0
    entity._updated_at = 0.0

    session.rollback()

    assert session.state == "rolled_back"
    assert session.active is False
    assert doc.get("t", "e1") is entity
    assert external_reference is doc.get("t", "e1")
    assert {
        key: value
        for key, value in entity.__dict__.items()
        if key not in ("_validator", "_document_owner")
    } == before
    assert entity._validator is original_validator
    assert entity._document_owner is original_owner is doc
    assert entity.version == 2
    assert entity.deleted is False
    assert entity.actor_log() == before["_actor_log"]
    assert entity.provenance("x") == provenance


def test_mutation_snapshot_preserves_document_owner_identity_without_deepcopy(monkeypatch):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    entity = doc.add(Entity(type="t", id="e", data={}))

    def forbidden_deepcopy(self, memo):
        raise AssertionError("Document owner must never be deep-copied")

    monkeypatch.setattr(Document, "__deepcopy__", forbidden_deepcopy, raising=False)
    snapshot = doc.capture_mutation_snapshot()
    entity._document_owner = object()

    doc.restore_mutation_snapshot(snapshot)

    assert entity._document_owner is doc


def test_mutation_session_removes_new_entities_and_restores_hard_removed_originals():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    empty_bucket = doc._collections["empty"]
    original = Entity(type="t", id="same-id", data={"value": "original"})
    doc.add(original)
    collections = doc._collections
    t_bucket = doc._collections["t"]
    before_keys = list(collections)
    session = doc.begin_mutation_session()

    doc.hard_remove("t", "same-id")
    replacement = Entity(type="t", id="same-id", data={"value": "replacement"})
    replacement_schema = replacement._schema
    replacement_validator = replacement._validator
    doc.add(replacement)
    created = Entity(type="new-type", id="new-id", data={"created": True})
    doc.add(created)
    doc._collections["empty"]["temporary"] = Entity(
        type="empty", id="temporary", data={},
    )

    session.rollback()

    assert doc._collections is collections
    assert list(doc._collections) == before_keys
    assert doc._collections["t"] is t_bucket
    assert doc._collections["empty"] is empty_bucket
    assert doc.get("t", "same-id") is original
    assert doc.get("t", "same-id") is not replacement
    assert doc.find("new-type", "new-id") is None
    assert doc.find("empty", "temporary") is None
    assert replacement._document_owner is None
    assert replacement._schema is replacement_schema
    assert replacement._validator is replacement_validator
    assert created._document_owner is None

    other = Document(schema_version="1.0.0", collaboration_mode="with_save")
    other.add(replacement)
    assert other.get("t", "same-id") is replacement


def test_nested_mutation_sessions_keep_independent_boundaries():
    actor = Actor(user_id="u1")
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    entity = Entity(type="t", id="e1", data={"x": 1})
    doc.add(entity)

    outer = doc.begin_mutation_session()
    entity.update({"x": 2}, expected_version=entity.version, actor=actor)
    inner = doc.begin_mutation_session()
    entity.update({"x": 3}, expected_version=entity.version, actor=actor)

    inner.commit()
    assert inner.state == "committed"
    assert entity.data["x"] == 3

    # 内层 commit 只关闭自己的快照；外层仍能撤销整个批次到 x=1。
    outer.rollback()
    assert outer.state == "rolled_back"
    assert doc.get("t", "e1") is entity
    assert entity.data["x"] == 1
    assert entity.version == 1


def test_inner_rollback_restores_inner_boundary_then_outer_can_commit():
    actor = Actor(user_id="u1")
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    entity = Entity(type="t", id="e1", data={"x": 1})
    doc.add(entity)

    outer = doc.begin_mutation_session()
    entity.update({"x": 2}, expected_version=entity.version, actor=actor)
    inner = doc.begin_mutation_session()
    entity.update({"x": 3}, expected_version=entity.version, actor=actor)
    inner.rollback()

    assert entity.data["x"] == 2
    assert entity.version == 2
    outer.commit()
    assert outer.state == "committed"
    assert entity.data["x"] == 2


def test_nested_mutation_sessions_reject_out_of_order_finalize():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    outer = doc.begin_mutation_session()
    inner = doc.begin_mutation_session()

    with pytest.raises(ZeError) as exc:
        outer.rollback()
    assert exc.value.code == "mutation_session_order_violation"
    assert outer.state == "active"

    inner.commit()
    outer.rollback()
    assert inner.state == "committed"
    assert outer.state == "rolled_back"


def test_exclusive_mutation_session_rejects_outer_and_later_sessions():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    outer = doc.begin_mutation_session()
    with pytest.raises(ZeError) as nested_exclusive:
        doc.begin_mutation_session(exclusive=True)
    assert nested_exclusive.value.code == "mutation_session_busy"
    assert nested_exclusive.value.context["reason"] == "exclusive_mutation_session_required"
    outer.rollback()

    exclusive = doc.begin_mutation_session(exclusive=True)
    with pytest.raises(ZeError) as later:
        doc.begin_mutation_session()
    assert later.value.code == "mutation_session_busy"
    assert later.value.context["reason"] == "mutation_session_owned_elsewhere"
    exclusive.commit()

    ordinary = doc.begin_mutation_session()
    ordinary.rollback()


def test_exclusive_mutation_session_forbids_document_save_before_io(tmp_path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    target = tmp_path / "uncreated" / "document.json"
    exclusive = doc.begin_mutation_session(exclusive=True)
    try:
        with pytest.raises(ZeError) as error:
            doc.save(target)
        assert error.value.code == "document_save_forbidden"
        assert error.value.context["reason"] == "exclusive_mutation_session_active"
        assert proven_document_write_outcome(error.value) == ("not_durable", None)
        assert not target.parent.exists()
    finally:
        exclusive.rollback()


def test_atomic_commit_candidate_requires_exact_exclusive_session_and_rolls_back():
    doc = Document(
        schema_version="1.0.0", collaboration_mode="with_save", id="candidate-doc",
    )
    before_version = doc.version
    before_updated_at = doc.updated_at

    ordinary = doc.begin_mutation_session()
    with pytest.raises(ZeError) as nonexclusive:
        ordinary._prepare_atomic_commit_candidate(
            before_version,
            _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
        )
    assert nonexclusive.value.code == "mutation_session_exclusive_required"
    ordinary.rollback()

    session = doc.begin_mutation_session(exclusive=True)
    with pytest.raises(ZeError) as forbidden:
        session._prepare_atomic_commit_candidate(before_version)
    assert forbidden.value.code == "document_atomic_commit_forbidden"
    with pytest.raises(ZeError) as stale:
        session._prepare_atomic_commit_candidate(
            before_version + 1,
            _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
        )
    assert stale.value.code == "version_conflict"

    candidate = session._prepare_atomic_commit_candidate(
        before_version,
        _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
    )
    verified = document_module._verify_atomic_document_commit_candidate(
        candidate,
        doc,
        session,
        _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
    )
    assert verified.document_id == doc.id
    assert verified.before_version == before_version
    assert verified.document_version == before_version + 1 == doc.version
    assert verified.updated_at == doc.updated_at >= before_updated_at
    with pytest.raises(ZeError) as duplicate:
        session._prepare_atomic_commit_candidate(
            doc.version,
            _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
        )
    assert duplicate.value.code == "document_commit_candidate_already_issued"
    with pytest.raises(TypeError):
        document_module._DocumentCommitCandidate()
    with pytest.raises(TypeError):
        copy.copy(candidate)

    session.rollback()
    assert doc.version == before_version
    assert doc.updated_at == before_updated_at
    with pytest.raises(ZeError) as closed:
        document_module._verify_atomic_document_commit_candidate(
            candidate,
            doc,
            session,
            _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
        )
    assert closed.value.code == "document_commit_candidate_invalid"


def test_atomic_commit_candidate_commit_keeps_exact_version_and_is_thread_bound():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    session = doc.begin_mutation_session(exclusive=True)
    candidate = session._prepare_atomic_commit_candidate(
        doc.version,
        _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
    )
    expected = (doc.version, doc.updated_at)
    observed: list[str] = []

    def verify_from_other_thread() -> None:
        try:
            document_module._verify_atomic_document_commit_candidate(
                candidate,
                doc,
                session,
                _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
            )
        except ZeError as exc:
            observed.append(exc.code)

    worker = threading.Thread(target=verify_from_other_thread)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert observed == ["mutation_session_thread_mismatch"]

    session.commit()
    assert (doc.version, doc.updated_at) == expected
    with pytest.raises(ZeError) as closed:
        document_module._verify_atomic_document_commit_candidate(
            candidate,
            doc,
            session,
            _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
        )
    assert closed.value.code == "document_commit_candidate_invalid"


def test_atomic_commit_candidate_rejects_forgery_and_invalid_clock_without_state_change(
    monkeypatch,
):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    session = doc.begin_mutation_session(exclusive=True)
    before = (doc.version, doc.updated_at)

    forged = object.__new__(document_module._DocumentCommitCandidate)
    with pytest.raises(ZeError) as invalid:
        document_module._verify_atomic_document_commit_candidate(
            forged,
            doc,
            session,
            _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
        )
    assert invalid.value.code == "document_commit_candidate_invalid"

    monkeypatch.setattr(document_module.time, "time", lambda: float("nan"))
    with pytest.raises(ZeError) as clock:
        session._prepare_atomic_commit_candidate(
            doc.version,
            _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
        )
    assert clock.value.code == "document_commit_clock_invalid"
    assert (doc.version, doc.updated_at) == before
    session.rollback()


def test_atomic_commit_candidate_tamper_blocks_commit_but_keeps_rollback_authority():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    session = doc.begin_mutation_session(exclusive=True)
    before = (doc.version, doc.updated_at)
    session._prepare_atomic_commit_candidate(
        doc.version,
        _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
    )
    doc._v += 1

    with pytest.raises(ZeError) as invalid:
        session.commit()
    assert invalid.value.code == "document_commit_candidate_invalid"
    session.rollback()
    assert (doc.version, doc.updated_at) == before


def test_atomic_commit_candidate_is_invalid_in_fork_child_but_parent_can_finish():
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("fork start method unavailable")
    context = multiprocessing.get_context("fork")
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    session = doc.begin_mutation_session(exclusive=True)
    before_version = doc.version
    candidate = session._prepare_atomic_commit_candidate(
        before_version,
        _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
    )
    queue = context.Queue()
    child = context.Process(
        target=_fork_atomic_candidate_probe,
        args=(doc, session, candidate, queue),
    )
    child.start()
    outcome = queue.get(timeout=10)
    child.join(10)
    assert child.exitcode == 0
    assert outcome == ("error", "mutation_session_process_mismatch")

    verified = document_module._verify_atomic_document_commit_candidate(
        candidate,
        doc,
        session,
        _capability=document_module._ATOMIC_DOCUMENT_COMMIT_CAPABILITY,
    )
    assert verified.document_version == before_version + 1
    session.rollback()
    assert doc.version == before_version


def test_mutation_sessions_reject_cross_thread_overlap():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    entered = threading.Event()
    release = threading.Event()
    result = {}

    def hold_session():
        session = doc.begin_mutation_session()
        entered.set()
        assert release.wait(5)
        session.rollback()

    worker = threading.Thread(target=hold_session)
    worker.start()
    assert entered.wait(5)
    try:
        with pytest.raises(ZeError) as overlap:
            doc.begin_mutation_session()
        assert overlap.value.code == "mutation_session_busy"
        assert overlap.value.context["reason"] == "mutation_session_owned_elsewhere"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


def test_mutation_session_commit_is_one_shot_and_keeps_mutation():
    actor = Actor(user_id="u1")
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    entity = Entity(type="t", id="e1", data={"x": 1})
    doc.add(entity)
    session = doc.begin_mutation_session()
    assert isinstance(session, DocumentMutationSession)
    assert session.state == "active"
    assert session.active is True

    entity.update({"x": 2}, expected_version=entity.version, actor=actor)
    session.commit()

    assert entity.data["x"] == 2
    assert session.state == "committed"
    assert session.active is False
    for finalize in (session.commit, session.rollback):
        with pytest.raises(ZeError) as exc:
            finalize()
        assert exc.value.code == "mutation_session_closed"
        assert exc.value.context["state"] == "committed"


def test_mutation_session_rollback_is_one_shot():
    actor = Actor(user_id="u1")
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    entity = Entity(type="t", id="e1", data={"x": 1})
    doc.add(entity)
    session = doc.begin_mutation_session()
    entity.update({"x": 2}, expected_version=entity.version, actor=actor)

    assert session.rollback() is None
    assert entity.data["x"] == 1
    for finalize in (session.rollback, session.commit):
        with pytest.raises(ZeError) as exc:
            finalize()
        assert exc.value.code == "mutation_session_closed"
        assert exc.value.context["state"] == "rolled_back"


def test_mutation_snapshot_rejects_cross_document_restore_even_with_same_id():
    first = Document(
        schema_version="1.0.0", collaboration_mode="with_save", id="same-doc-id",
    )
    second = Document(
        schema_version="1.0.0", collaboration_mode="with_save", id="same-doc-id",
        name="second-must-not-change",
    )
    snapshot = first.capture_mutation_snapshot()

    with pytest.raises(ZeError) as exc:
        second.restore_mutation_snapshot(snapshot)

    assert exc.value.code == "mutation_snapshot_owner_mismatch"
    assert second.name == "second-must-not-change"
    assert second.id == first.id


def test_restore_mutation_snapshot_rejects_wrong_token_type():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    with pytest.raises(ZeError) as exc:
        doc.restore_mutation_snapshot({})  # type: ignore[arg-type]
    assert exc.value.code == "validation_failed"


def test_mutation_session_wraps_restore_failure_and_becomes_terminal(monkeypatch):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    session = doc.begin_mutation_session()

    def broken_restore(_self, snapshot):
        raise RuntimeError("restore exploded")

    monkeypatch.setattr(type(doc), "restore_mutation_snapshot", broken_restore)
    with pytest.raises(ZeError) as exc:
        session.rollback()

    assert exc.value.code == "mutation_rollback_failed"
    assert exc.value.context["error_type"] == "RuntimeError"
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert session.state == "rollback_failed"
    assert session.active is False
    with pytest.raises(ZeError) as closed:
        session.rollback()
    assert closed.value.code == "mutation_session_closed"


# ==============================================================
# Strict value ownership + durable collection order
# ==============================================================

def test_document_nested_metadata_is_owned_and_getters_are_detached():
    schema = {"card": {"type": "object", "properties": {"tags": {"type": "array"}}}}
    config = {"editor": {"panels": ["left", "right"]}}
    acl = {
        "owner": "u1",
        "collaborators": ["u2"],
        "rules": {"groups": ["artists"]},
    }
    harvest_rules = [{"when_role": "ze.field.media@1", "supply_required": {"kind": "image"}}]
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema=schema,
        config=config,
        acl=acl,
        harvest_rules=harvest_rules,
    )

    schema["card"]["properties"]["tags"]["type"] = "string"
    config["editor"]["panels"].append("hidden")
    acl["collaborators"].append("intruder")
    acl["rules"]["groups"].append("admins")
    harvest_rules[0]["supply_required"]["kind"] = "video"

    assert doc.schema["card"]["properties"]["tags"]["type"] == "array"
    assert doc.config["editor"]["panels"] == ["left", "right"]
    assert doc.acl["collaborators"] == ["u2"]
    assert doc.acl["rules"]["groups"] == ["artists"]
    assert doc.harvest_rules[0]["supply_required"]["kind"] == "image"

    schema_out = doc.schema
    config_out = doc.config
    acl_out = doc.acl
    rules_out = doc.harvest_rules
    schema_out["card"]["properties"].clear()
    config_out["editor"]["panels"].clear()
    acl_out["rules"]["groups"].clear()
    rules_out[0]["supply_required"].clear()

    assert "tags" in doc.schema["card"]["properties"]
    assert doc.config["editor"]["panels"] == ["left", "right"]
    assert doc.acl["rules"]["groups"] == ["artists"]
    assert doc.harvest_rules[0]["supply_required"] == {"kind": "image"}


def test_set_acl_owns_input_and_returns_detached_previous_value():
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        acl={"owner": "u1", "collaborators": [], "rules": {"groups": ["old"]}},
    )
    replacement = {
        "owner": "u1",
        "collaborators": ["u2"],
        "rules": {"groups": ["new"]},
    }
    previous = doc.set_acl(replacement)
    replacement["rules"]["groups"].append("intruder")
    previous["rules"]["groups"].append("mutated-copy")
    assert doc.acl["rules"]["groups"] == ["new"]
    assert doc.acl["collaborators"] == ["u2"]


def test_canonical_entities_always_precede_read_only_overlay():
    def build(source_first: bool) -> Document:
        doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
        spec = ReadOnlySourceSpec(
            name="overlay",
            entity_type="card",
            loader=lambda: [
                {"id": "ro-1", "data": {"label": "one"}},
                {"id": "ro-2", "data": {"label": "two"}},
            ],
        )
        if source_first:
            doc.register_read_only_source(spec)
        doc.add(Entity(type="card", id="z", data={}))
        doc.add(Entity(type="card", id="a", data={}))
        if not source_first:
            doc.register_read_only_source(spec)
        return doc

    expected = ["z", "a", "ro-1", "ro-2"]
    assert [entity.id for entity in build(True).list("card")] == expected
    assert [entity.id for entity in build(False).list("card")] == expected


def test_read_only_fallback_schema_is_canonical_and_owner_bound():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.register_read_only_source(ReadOnlySourceSpec(
        name="overlay",
        entity_type="card",
        schema={"title": {"type": "string"}},
        loader=lambda: [{"id": "ro", "data": {"title": "remote"}}],
    ))

    canonical = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
    }
    entity = doc.get("card", "ro")
    assert doc._read_only_sources["overlay"].schema == canonical
    assert entity._schema == canonical
    assert entity._document_owner is doc


def test_legacy_save_open_preserves_canonical_insertion_order(tmp_path: Path):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    for entity_id in ("z", "a", "m"):
        doc.add(Entity(type="card", id=entity_id, data={"id": entity_id}))

    path = tmp_path / "document.json"
    doc.save(path)
    raw = json.loads(path.read_text())
    assert raw["collections"]["card"] == ["z", "a", "m"]

    reopened = Document.open(path)
    assert [entity.id for entity in reopened.list("card")] == ["z", "a", "m"]


def test_mutation_snapshot_rejects_unknown_value_without_running_copy_hooks():
    hooks = []

    class CopyTrap:
        def __deepcopy__(self, memo):
            hooks.append("deepcopy")
            raise AssertionError("deepcopy hook must not run")

        def __repr__(self):
            hooks.append("repr")
            raise AssertionError("repr hook must not run")

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    # Private corruption models a future field/extension bypassing the public
    # strict-JSON boundary.  Snapshot capture must fail closed, not execute it.
    doc._config["extension"] = CopyTrap()
    with pytest.raises(ZeError) as exc:
        doc.capture_mutation_snapshot()
    assert exc.value.code == "mutation_snapshot_failed"
    assert hooks == []


def test_mutation_snapshot_rejects_non_string_state_key_without_running_hooks():
    hooks = []

    class KeyTrap:
        def __hash__(self):
            hooks.append("hash")
            return 1

        def __eq__(self, other):
            hooks.append("eq")
            raise AssertionError("key comparison hook must not run")

        def __repr__(self):
            hooks.append("repr")
            raise AssertionError("key repr hook must not run")

    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    trap = KeyTrap()
    doc.__dict__[trap] = "corrupt"
    hooks.clear()

    with pytest.raises(ZeError) as exc:
        doc.capture_mutation_snapshot()

    assert exc.value.code == "mutation_snapshot_failed"
    assert hooks == []


def test_acl_rejects_container_subclasses_before_running_their_hooks():
    hooks = []

    class DictSubclass(dict):
        def get(self, key, default=None):
            hooks.append("dict.get")
            raise AssertionError("subclass hook must not run")

    class ListSubclass(list):
        def __iter__(self):
            hooks.append("list.iter")
            raise AssertionError("subclass hook must not run")

    with pytest.raises(ZeError) as dict_error:
        Document(
            schema_version="1.0.0",
            collaboration_mode="with_save",
            acl=DictSubclass({"owner": "u"}),
        )
    assert dict_error.value.code == "validation_failed"

    with pytest.raises(ZeError) as list_error:
        Document(
            schema_version="1.0.0",
            collaboration_mode="with_save",
            acl={"owner": "u", "collaborators": ListSubclass(["v"])},
        )
    assert list_error.value.code == "validation_failed"
    assert hooks == []


def test_mutation_snapshot_rejects_corrupt_provenance_without_alias_or_hooks():
    hooks = []

    class Trap:
        def __deepcopy__(self, memo):
            hooks.append("deepcopy")
            raise AssertionError("deepcopy hook must not run")

        def __repr__(self):
            hooks.append("repr")
            raise AssertionError("repr hook must not run")

    provenance = AiProvenance(actor="u", model="m")
    object.__setattr__(provenance, "actor", Trap())
    entity = Entity(type="card", id="c1", data={})
    entity._provenance["field"] = provenance
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(entity)

    with pytest.raises(ZeError) as exc:
        doc.capture_mutation_snapshot()
    assert exc.value.code == "mutation_snapshot_failed"
    assert hooks == []


def test_read_only_source_schema_uses_same_strict_domain_even_when_overridden():
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"card": {"type": "object"}},
    )
    spec = ReadOnlySourceSpec(
        name="bad-source-schema",
        entity_type="card",
        loader=lambda: [],
        schema={"maximum": 10 ** 4300},
    )
    with pytest.raises(ZeError) as exc:
        doc.register_read_only_source(spec)
    assert exc.value.code == "validation_failed"
    assert exc.value.context["reason"] == "read_only_source_value_invalid"
    assert doc.read_only_sources() == []


def test_patch_batch_failure_restores_middle_entity_order_and_all_metadata():
    from operation import ENTITY_META_FIELD, Patch

    actor = Actor(user_id="u")
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    entities = {}
    for entity_id in ("z", "a", "m"):
        entity = Entity(type="card", id=entity_id, data={"id": entity_id})
        entities[entity_id] = entity
        doc.add(entity)
    doc.register_read_only_source(ReadOnlySourceSpec(
        name="overlay",
        entity_type="card",
        loader=lambda: [{"id": "ro", "data": {}}],
    ))
    bucket = doc._collections["card"]
    before_order = [entity.id for entity in doc.list("card")]
    before_version = doc.version
    before_updated_at = doc.updated_at

    delete_middle = Patch(
        entity_type="card",
        entity_id="a",
        field=ENTITY_META_FIELD,
        op="delete",
        before=entities["a"].to_dict(),
        after=None,
    )
    late_failure = Patch(
        entity_type="card",
        entity_id="missing",
        field="x",
        op="set",
        before=None,
        after=1,
    )
    with pytest.raises(ZeError) as exc:
        doc.apply_patches_atomically(
            [delete_middle, late_failure], actor=actor,
            _replay_capability=_HISTORY_REPLAY_CAPABILITY,
        )
    assert exc.value.code == "entity_not_found"
    assert doc._collections["card"] is bucket
    assert doc.get("card", "a") is entities["a"]
    assert [entity.id for entity in doc.list("card")] == before_order
    assert before_order == ["z", "a", "m", "ro"]
    assert doc.version == before_version
    assert doc.updated_at == before_updated_at


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": []},
        {"id": 7},
        {"id": ""},
    ],
)
def test_document_rejects_non_persistable_scalar_metadata(kwargs):
    with pytest.raises(ZeError) as exc:
        Document(
            schema_version="1.0.0",
            collaboration_mode="with_save",
            **kwargs,
        )
    assert exc.value.code == "validation_failed"


# ---- secure head/metadata boundary ----

def test_document_metadata_is_detached_and_head_exposes_exact_locked_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="metadata-doc",
        schema_version="1.0.0",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
        schema={"card": {"title": {"type": "string"}}},
        config={"nested": {"items": [1]}},
        acl={"owner": "alice", "rules": {"groups": ["editors"]}},
    )
    document.add(Entity(type="card", id="row", data={"title": "hello"}))
    document.save(path)
    exact_payload = path.read_bytes()

    with open_document_head(path) as head:
        assert head is not None
        assert head.payload == exact_payload
        assert isinstance(head.payload, bytes)
        assert isinstance(head.metadata, DocumentMetadata)
        envelope = head.envelope
        envelope["acl"]["rules"]["groups"].append("attacker")
        assert head.envelope["acl"]["rules"]["groups"] == ["editors"]
        metadata = head.metadata

    assert metadata.id == "metadata-doc"
    assert metadata.version == document.version
    assert metadata.storage_version == "v2"
    assert metadata.effective_save_policy == "auto_commit"
    fragment_names = (
        "_data_schema_fragment",
        "_config_fragment",
        "_acl_fragment",
        "_collections_fragment",
        "_entity_storage_fragment",
        "_read_only_sources_fragment",
    )
    assert all(
        type(object.__getattribute__(metadata, member)) is bytes
        for member in fragment_names
    )
    assert not hasattr(metadata, "_payload")
    metadata.acl["rules"]["groups"].append("attacker")
    metadata.config["nested"]["items"].append(2)
    metadata.collections["card"].append("invented")
    assert metadata.acl["rules"]["groups"] == ["editors"]
    assert metadata.config == {"nested": {"items": [1]}}
    assert metadata.collections == {"card": ["row"]}
    assert path.read_bytes() == exact_payload


def test_metadata_nested_getters_decode_only_their_validated_fragments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small auth/config read must not repeatedly parse a large head."""
    path = tmp_path / "document.json"
    Document(
        id="fragmented-metadata",
        schema_version="1.0.0",
        collaboration_mode="with_save",
        config={"nested": {"items": [1]}},
        acl={"owner": "alice", "rules": {"groups": ["editors"]}},
    ).save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Both an ordinary large collections index and a forward-compatible opaque
    # extension are unrelated to ACL/config reads and must not be reparsed by
    # those getters.  Metadata-only reading intentionally does zero Entity I/O.
    raw["collections"] = {
        "card": [f"row-{index:05d}" for index in range(6_000)],
    }
    raw["future_extension"] = {"opaque": "x" * (512 * 1024)}
    path.write_text(
        json.dumps(raw, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    full_payload = path.read_bytes()
    metadata = read_document_metadata(path)

    decoded: list[tuple[str, int]] = []
    real_decode = document_module._decode_persisted_json

    def observed_decode(payload: bytes, *, member: str):
        decoded.append((member, len(payload)))
        return real_decode(payload, member=member)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            document_module,
            "_decode_persisted_json",
            observed_decode,
        )
        acl = metadata.acl
        config = metadata.config
        acl["rules"]["groups"].append("attacker")
        config["nested"]["items"].append(2)
        assert metadata.acl["rules"]["groups"] == ["editors"]
        assert metadata.config == {"nested": {"items": [1]}}

    assert {member for member, _size in decoded} == {
        "document_acl",
        "document_config",
    }
    assert decoded
    assert max(size for _member, size in decoded) < len(full_payload) // 100
    assert len(metadata.collections["card"]) == 6_000
    with open_document_head(path) as head:
        assert head is not None
        assert head.payload == full_payload
        assert head.envelope["future_extension"] == raw["future_extension"]
    assert path.read_bytes() == full_payload


def test_metadata_fragments_preserve_defaults_and_durable_representation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.json"
    Document(
        id="fragment-semantics",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    ).save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("acl")
    raw.pop("config")
    raw["data_schema"] = {"card": {"title": {"type": "string"}}}
    raw["read_only_sources"] = [
        {
            "name": "external",
            "entity_type": "external_card",
            "future": {"kept": [1, 2]},
        },
    ]
    raw["future_extension"] = {"kept": True}
    path.write_text(
        json.dumps(raw, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    metadata = read_document_metadata(path)
    assert metadata.acl == {}
    assert metadata.config == {}
    assert metadata.data_schema == raw["data_schema"]
    assert metadata.read_only_sources == raw["read_only_sources"]
    with open_document_head(path) as head:
        assert head is not None
        assert head.envelope["future_extension"] == {"kept": True}
    assert Document.open(path).schema["card"] == {
        "type": "object",
        "properties": {"title": {"type": "string"}},
    }


def test_open_document_head_drops_parser_working_graph_before_yield(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    Document(
        id="head-working-graph",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    ).save(path)
    references: list[weakref.ReferenceType] = []
    real_parse = document_module._parse_document_head

    def observed_parse(payload: bytes):
        parsed = real_parse(payload)
        references.append(weakref.ref(parsed))
        return parsed

    monkeypatch.setattr(document_module, "_parse_document_head", observed_parse)
    with open_document_head(path) as head:
        assert head is not None
        gc.collect()
        assert references and references[-1]() is None
        assert head.metadata.id == "head-working-graph"


def test_validated_head_values_cannot_be_forged_through_public_constructors(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.json"
    Document(
        id="sealed-head",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    ).save(path)
    authentic = read_document_metadata(path)
    assert not hasattr(authentic, "_seal")

    with pytest.raises(TypeError):
        DocumentMetadata(
            id="forged",
            name="",
            schema="ze-document",
            schema_version="1.0.0",
            collaboration_mode="with_save",
            save_policy=None,
            created_at=0.0,
            updated_at=0.0,
            version=1,
            storage_version="legacy",
            _data_schema_fragment=b"{}",
            _config_fragment=b"{}",
            _acl_fragment=b"{}",
            _collections_fragment=b"{}",
            _entity_storage_fragment=b"{}",
            _read_only_sources_fragment=b"{}",
            _seal=object(),
        )
    from document import DocumentHead

    with pytest.raises(TypeError):
        DocumentHead(
            metadata=authentic,
            payload=path.read_bytes(),
            _seal=object(),
        )


@pytest.mark.parametrize(
    ("missing", "expected_created", "expected_updated"),
    [
        (("created_at", "updated_at"), 0.0, 0.0),
        (("created_at",), "updated_at", "updated_at"),
        (("updated_at",), "created_at", "created_at"),
    ],
)
def test_legacy_missing_timestamps_are_accepted_deterministically(
    tmp_path: Path,
    missing: tuple[str, ...],
    expected_created,
    expected_updated,
) -> None:
    path = tmp_path / "document.json"
    Document(
        id="legacy-timestamps",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    ).save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    original = dict(raw)
    for member in missing:
        del raw[member]
    path.write_text(json.dumps(raw), encoding="utf-8")
    exact_payload = path.read_bytes()

    first = read_document_metadata(path)
    second = read_document_metadata(path)
    reopened = Document.open(path)
    created = (
        original[expected_created]
        if type(expected_created) is str
        else expected_created
    )
    updated = (
        original[expected_updated]
        if type(expected_updated) is str
        else expected_updated
    )
    assert (first.created_at, first.updated_at) == (created, updated)
    assert second == first
    assert (reopened.created_at, reopened.updated_at) == (created, updated)
    assert path.read_bytes() == exact_payload


def test_metadata_readers_perform_zero_entity_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="metadata-no-entity-io",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.add(Entity(type="card", id="row", data={"value": 1}))
    document.save(path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("metadata boundary must not read an Entity")

    monkeypatch.setattr(EntityFileStore, "read_v2_entity", forbidden)
    monkeypatch.setattr(EntityFileStore, "read_legacy_entity", forbidden)
    metadata = read_document_metadata(path)
    assert metadata.collections == {"card": ["row"]}
    with open_document_head(path) as head:
        assert head is not None
        assert head.metadata.id == "metadata-no-entity-io"


@pytest.mark.parametrize(
    ("member", "bad"),
    [
        ("id", None),
        ("schema", "not-ze-document"),
        ("schema", None),
        ("schema_version", ""),
        ("collaboration_mode", "not-a-mode"),
        ("save_policy", "not-a-policy"),
        ("save_policy", None),
        ("created_at", -1),
        ("updated_at", -1),
        ("acl", []),
        ("config", []),
        ("entity_storage", {"schema": "wrong", "layout": "wrong"}),
        ("collections", []),
        ("read_only_sources", [{"name": "missing-type"}]),
    ],
)
def test_metadata_and_full_open_share_one_bad_head_truth(
    tmp_path: Path,
    member: str,
    bad,
) -> None:
    path = tmp_path / "document.json"
    Document(
        id="shared-parser",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    ).save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[member] = bad
    path.write_text(
        json.dumps(raw, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    diagnoses = []
    for reader in (read_document_metadata, Document.open):
        with pytest.raises(ZeError) as captured:
            reader(path)
        diagnoses.append((captured.value.code, captured.value.context["reason"]))
    assert diagnoses[0] == diagnoses[1]


@pytest.mark.parametrize(
    ("collections", "reason"),
    [
        ({"bad/type": []}, "legacy_component_invalid"),
        ({".": []}, "legacy_component_invalid"),
        ({"..": []}, "legacy_component_invalid"),
        ({"bad\x00type": []}, "legacy_component_invalid"),
        ({ENTITY_STORE_DIRECTORY: []}, "legacy_type_reserved"),
        ({"card": ["bad/id"]}, "legacy_component_invalid"),
        ({"card": ["."]}, "legacy_component_invalid"),
        ({"card": [".."]}, "legacy_component_invalid"),
        ({"card": ["bad\x00id"]}, "legacy_component_invalid"),
    ],
)
def test_metadata_and_full_open_share_legacy_collection_path_truth(
    tmp_path: Path,
    collections: dict[str, list[str]],
    reason: str,
) -> None:
    """Legacy collections are path semantics even when a bucket is empty."""
    path = tmp_path / "document.json"
    Document(
        id="legacy-index-parser",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    ).save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("entity_storage")
    raw["collections"] = collections
    path.write_text(
        json.dumps(raw, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )

    diagnoses = []
    for reader in (read_document_metadata, Document.open):
        with pytest.raises(ZeError) as captured:
            reader(path)
        diagnoses.append(
            (
                captured.value.code,
                captured.value.context.get("reason"),
                captured.value.context.get("commit_point_reached"),
            )
        )
    assert diagnoses == [
        ("document_storage_layout_invalid", reason, False),
        ("document_storage_layout_invalid", reason, False),
    ]


@pytest.mark.parametrize("corruption", ["duplicate-key", "nan"])
def test_metadata_and_full_open_share_strict_json_rejection(
    tmp_path: Path,
    corruption: str,
) -> None:
    path = tmp_path / "document.json"
    Document(
        id="strict-json-head",
        name="original",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    ).save(path)
    text = path.read_text(encoding="utf-8")
    if corruption == "duplicate-key":
        needle = '"name": "original",'
        assert needle in text
        text = text.replace(needle, needle + '\n  "name": "duplicate",', 1)
    else:
        needle = '"config": {},'
        assert needle in text
        text = text.replace(needle, '"config": {"bad": NaN},', 1)
    path.write_text(text, encoding="utf-8")

    diagnoses = []
    for reader in (read_document_metadata, Document.open):
        with pytest.raises(ZeError) as captured:
            reader(path)
        diagnoses.append((captured.value.code, captured.value.context["reason"]))
    assert diagnoses == [
        ("project_recovery_required", "document_json_invalid"),
        ("project_recovery_required", "document_json_invalid"),
    ]


def test_metadata_reader_enforces_document_byte_limit_before_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import entity_file_store as entity_file_store_module

    path = tmp_path / "document.json"
    Document(
        id="oversize-head",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    ).save(path)
    monkeypatch.setattr(
        entity_file_store_module,
        "MAX_DOCUMENT_FILE_BYTES",
        len(path.read_bytes()) - 1,
    )
    with pytest.raises(ZeError) as captured:
        read_document_metadata(path)
    assert captured.value.code == "document_storage_layout_invalid"
    assert captured.value.context["reason"] == "file_size_invalid"


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo"])
def test_metadata_reader_rejects_unsafe_document_inode_without_path_leak(
    tmp_path: Path,
    attack: str,
) -> None:
    path = tmp_path / "document.json"
    Document(
        id="unsafe-head",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    ).save(path)
    payload = path.read_bytes()
    path.unlink()
    sentinel = tmp_path / "sentinel.json"
    if attack in ("symlink", "hardlink"):
        sentinel.write_bytes(payload)
        if attack == "symlink":
            path.symlink_to(sentinel)
        else:
            os.link(sentinel, path)
    else:
        os.mkfifo(path, 0o600)

    with pytest.raises(ZeError) as captured:
        read_document_metadata(path)
    assert captured.value.code == "document_storage_path_unsafe"
    assert str(tmp_path) not in str(captured.value)
    if sentinel.exists():
        assert sentinel.read_bytes() == payload


def test_open_document_head_missing_ok_is_decided_under_requested_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    path = project / "document.json"
    calls: list[str] = []
    real_exclusive = EntityFileStore.acquire_exclusive_lock

    def observed_exclusive(store):
        calls.append("exclusive")
        return real_exclusive(store)

    monkeypatch.setattr(
        EntityFileStore,
        "acquire_exclusive_lock",
        observed_exclusive,
    )
    with open_document_head(path, exclusive=True, missing_ok=True) as head:
        assert head is None
        assert calls == ["exclusive"]
    with pytest.raises(ZeError) as captured:
        read_document_metadata(path)
    assert captured.value.code == "project_recovery_required"
    assert captured.value.context["reason"] == "document_head_missing"


def test_rewrite_document_metadata_changes_only_allowed_values_without_drift(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="source-id",
        name="source-name",
        schema_version="1.0.0",
        collaboration_mode="with_save",
        save_policy="checkpoint_commit",
        config={"old": {"items": [1]}},
        acl={"owner": "alice", "collaborators": ["bob"]},
    )
    document.add(Entity(type="card", id="row", data={"value": 1}))
    document.save(path)
    before = json.loads(path.read_text(encoding="utf-8"))
    before["future_extension"] = {"nested": [1, {"kept": True}]}
    before["read_only_sources"] = [
        {"name": "external", "entity_type": "external", "future": [1, 2]}
    ]
    path.write_text(
        json.dumps(before, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    new_acl = {
        "owner": "carol",
        "collaborators": ["dave"],
        "rules": {"teams": ["art"]},
    }
    new_config = {"new": {"items": [2, 3]}}

    metadata = _rewrite_document_metadata_for_topology_staging(
        path,
        expected_version=document.version,
        id="target-id",
        name="target-name",
        acl=new_acl,
        config=new_config,
        _capability=_TOPOLOGY_STAGING_METADATA_CAPABILITY,
        _expected_root_identity=(path.parent.stat().st_dev, path.parent.stat().st_ino),
    )
    new_acl["rules"]["teams"].append("attacker")
    new_config["new"]["items"].append(4)
    after = json.loads(path.read_text(encoding="utf-8"))

    assert metadata.id == "target-id"
    assert metadata.name == "target-name"
    assert metadata.version == document.version
    assert metadata.acl["rules"]["teams"] == ["art"]
    assert metadata.config == {"new": {"items": [2, 3]}}
    assert set(after) == set(before)
    for member, value in before.items():
        if member not in {"id", "name", "acl", "config"}:
            assert after[member] == value
    assert after["id"] == "target-id"
    assert after["name"] == "target-name"
    assert after["acl"] == metadata.acl
    assert after["config"] == metadata.config
    reopened = Document.open(path)
    assert reopened.id == "target-id"
    assert reopened.get("card", "row").data == {"value": 1}


def test_rewrite_document_metadata_has_zero_entity_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="rewrite-no-entity-io",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.add(Entity(type="card", id="row", data={"value": 1}))
    document.save(path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("metadata rewrite must not touch Entity storage")

    monkeypatch.setattr(EntityFileStore, "read_v2_entity", forbidden)
    monkeypatch.setattr(EntityFileStore, "read_legacy_entity", forbidden)
    monkeypatch.setattr(EntityFileStore, "write_generation", forbidden)
    result = _rewrite_document_metadata_for_topology_staging(
        path,
        expected_version=document.version,
        name="rewritten",
        _capability=_TOPOLOGY_STAGING_METADATA_CAPABILITY,
        _expected_root_identity=(path.parent.stat().st_dev, path.parent.stat().st_ino),
    )
    assert result.name == "rewritten"


def test_rewrite_document_metadata_stale_cas_is_proven_not_durable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="rewrite-cas",
        name="before",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.save(path)
    before = path.read_bytes()

    with pytest.raises(ZeError) as captured:
        _rewrite_document_metadata_for_topology_staging(
            path,
            expected_version=document.version - 1,
            name="must-not-commit",
            _capability=_TOPOLOGY_STAGING_METADATA_CAPABILITY,
            _expected_root_identity=(path.parent.stat().st_dev, path.parent.stat().st_ino),
        )
    assert captured.value.code == "version_conflict"
    assert proven_document_write_outcome(captured.value) == ("not_durable", None)
    assert path.read_bytes() == before


def test_rewrite_document_metadata_rejects_empty_or_unlisted_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="rewrite-fields",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.save(path)
    before = path.read_bytes()

    with pytest.raises(ZeError) as forbidden:
        _rewrite_document_metadata_for_topology_staging(
            path,
            expected_version=document.version,
            name="live-shortcut",
        )
    assert forbidden.value.code == "topology_metadata_rewrite_forbidden"
    assert proven_document_write_outcome(forbidden.value) == (
        "not_durable",
        None,
    )
    with pytest.raises(ZeError) as unpinned:
        _rewrite_document_metadata_for_topology_staging(
            path,
            expected_version=document.version,
            name="unpinned-shortcut",
            _capability=_TOPOLOGY_STAGING_METADATA_CAPABILITY,
        )
    assert unpinned.value.code == "topology_metadata_rewrite_forbidden"
    assert unpinned.value.context["reason"] == (
        "topology_staging_root_identity_required"
    )
    assert proven_document_write_outcome(unpinned.value) == ("not_durable", None)
    with pytest.raises(ZeError) as captured:
        _rewrite_document_metadata_for_topology_staging(
            path,
            expected_version=document.version,
            _capability=_TOPOLOGY_STAGING_METADATA_CAPABILITY,
            _expected_root_identity=(path.parent.stat().st_dev, path.parent.stat().st_ino),
        )
    assert proven_document_write_outcome(captured.value) == ("not_durable", None)
    with pytest.raises(TypeError):
        _rewrite_document_metadata_for_topology_staging(
            path,
            expected_version=document.version,
            schema_version="2.0.0",  # type: ignore[call-arg]
            _capability=_TOPOLOGY_STAGING_METADATA_CAPABILITY,
        )
    assert path.read_bytes() == before


def test_rewrite_document_metadata_precommit_failure_is_proven_not_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="rewrite-precommit",
        name="before",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.save(path)
    before = path.read_bytes()

    def fail_before_rename(_store, _payload):
        raise EntityFileStoreError(
            "document_write_failed",
            "temporary_write_failed",
            committed=False,
        )

    monkeypatch.setattr(EntityFileStore, "commit_document", fail_before_rename)
    with pytest.raises(ZeError) as captured:
        _rewrite_document_metadata_for_topology_staging(
            path,
            expected_version=document.version,
            name="not-visible",
            _capability=_TOPOLOGY_STAGING_METADATA_CAPABILITY,
            _expected_root_identity=(path.parent.stat().st_dev, path.parent.stat().st_ino),
        )
    assert proven_document_write_outcome(captured.value) == ("not_durable", None)
    assert path.read_bytes() == before


def test_rewrite_document_metadata_postrename_failure_never_returns_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    document = Document(
        id="rewrite-postrename",
        name="before",
        schema_version="1.0.0",
        collaboration_mode="with_save",
    )
    document.save(path)
    real_commit = EntityFileStore.commit_document

    def fail_after_rename(store, payload):
        real_commit(store, payload)
        raise EntityFileStoreError(
            "document_durability_unknown",
            "project_directory_fsync_failed",
            committed=True,
        )

    monkeypatch.setattr(EntityFileStore, "commit_document", fail_after_rename)
    with pytest.raises(ZeError) as captured:
        _rewrite_document_metadata_for_topology_staging(
            path,
            expected_version=document.version,
            name="visible-but-unknown",
            _capability=_TOPOLOGY_STAGING_METADATA_CAPABILITY,
            _expected_root_identity=(path.parent.stat().st_dev, path.parent.stat().st_ino),
        )
    assert proven_document_write_outcome(captured.value) == (
        "unknown",
        document.version,
    )
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == (
        "visible-but-unknown"
    )


def test_document_rejects_string_subclasses_for_version_mode_and_policy():
    class StringSubclass(str):
        pass

    attempts = [
        {"schema_version": StringSubclass("1.0.0"), "collaboration_mode": "with_save"},
        {"schema_version": "1.0.0", "collaboration_mode": StringSubclass("with_save")},
        {
            "schema_version": "1.0.0",
            "collaboration_mode": "with_save",
            "save_policy": StringSubclass("auto_commit"),
        },
    ]
    for kwargs in attempts:
        with pytest.raises(ZeError) as exc:
            Document(**kwargs)
        assert exc.value.code == "validation_failed"
