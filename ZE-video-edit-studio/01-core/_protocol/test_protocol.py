"""test_protocol.py — 跨叶子协议 + audit gates 测试.

覆盖:
  - change_set.affected_entities() 5 种 change_kind 推导
  - change_set.to_broadcast_payload() 消息格式
  - EntityChange to_dict
  - audit_gates.run_all() 全部 PASS
  - audit_gates.run_one() / run_leaf()
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from change_set import (
    CHANGE_KINDS,
    EntityChange,
    affected_entities,
    to_broadcast_payload,
)
from audit_gates import (
    GATES,
    _find_raw_document_head_accesses,
    _find_private_topology_rewrite_uses,
    _gate_head_guard_spans,
    _history_authority_import_records,
    _history_authority_production_inventory,
    _history_authority_public_surface_violations,
    _pipeline_delegated_dormant_violations,
    check_ast_07_media_ticket_private_delivery,
    check_auth_09_operation_manual_trust_boundary,
    check_commit_02_production_surfaces_use_coordinator,
    check_commit_11_canonical_cutover_asset_members_and_phase_head,
    check_commit_12_sqlite_cutover_install_fixed_writer,
    check_commit_13_sqlite_cutover_plan_free_durable_audit,
    check_commit_14_sqlite_cutover_phase_one_head_finalizer,
    check_commit_15_sqlite_cutover_asset_provider_phase_writer,
    check_db_05_business_snapshot_root_and_restore,
    check_db_08_sqlite_legacy_import_schema_v3,
    check_db_10_sqlite_legacy_catalog_historical_seal_v4,
    check_db_12_sqlite_repository_exact_proof_v5,
    check_db_19_sqlite_mount_chronology_state_closure_v11,
    check_db_20_sqlite_cutover_install_closure_v12,
    check_doc_05_save_policy_declared,
    check_doc_07_safe_entity_file_store,
    check_doc_09_validated_head_authority,
    _iter_auditable_python_sources,
    check_project_05_stream_generation_cursor_fence,
    check_project_08_validated_head_consumers,
    check_project_09_strict_catalog_migration_barrier_and_fork_fence,
    check_project_10_repository_writer_admission_and_complete_state,
    check_project_11_same_barrier_exact_repository_proof,
    check_project_13_private_business_runtime_production_zero,
    check_pipe_09_delegated_mutation_dormant,
    check_rt_07_entity_status_snapshot_replaces,
    load_audit_surface_scope,
    run_all,
    run_leaf,
    run_one,
)

from document import Document
from entity import Actor, Entity
from operation import (
    ENTITY_META_FIELD,
    Patch,
    new_operation,
)


# ==============================================================
# 辅助
# ==============================================================

def _doc():
    return Document(schema_version="1.0.0", collaboration_mode="with_save")


def _actor():
    return Actor(user_id="u1")


def _write_surface_profile(root, *, primary, surfaces, entrypoints, **sections):
    payload = {
        "schema": "ze.surface_profile.v1",
        "primary": primary,
        "surfaces": surfaces,
        "entrypoints": entrypoints,
        **sections,
    }
    (root / "ZE_SURFACE.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


# ==============================================================
# EntityChange 数据结构
# ==============================================================

def test_entity_change_frozen():
    c = EntityChange(
        entity_type="t", entity_id="e1",
        change_kind="updated", changed_fields=("k",),
        patches=(), snapshot={"a": 1},
    )
    with pytest.raises(Exception):
        c.entity_type = "x"  # type: ignore[misc]


def test_entity_change_to_dict():
    p = Patch("t", "e1", "k", "set", before=1, after=2)
    c = EntityChange(
        entity_type="t", entity_id="e1",
        change_kind="updated", changed_fields=("k",),
        patches=(p,), snapshot={"id": "e1"},
    )
    d = c.to_dict()
    assert d["entity_type"] == "t"
    assert d["change_kind"] == "updated"
    assert d["changed_fields"] == ["k"]
    assert d["patches"][0]["field"] == "k"
    assert d["snapshot"] == {"id": "e1"}


def test_entity_change_to_dict_none_snapshot():
    """hard_removed 的 snapshot=None 应保留 None."""
    c = EntityChange(
        entity_type="t", entity_id="e1",
        change_kind="hard_removed", changed_fields=(ENTITY_META_FIELD,),
        patches=(), snapshot=None,
    )
    d = c.to_dict()
    assert d["snapshot"] is None


def test_change_kinds_5_states():
    assert set(CHANGE_KINDS) == {
        "created", "updated", "soft_deleted", "restored", "hard_removed"
    }


# ==============================================================
# affected_entities 推导
# ==============================================================

def test_aggregate_updated():
    doc = _doc()
    e = Entity(type="t", data={"x": 1, "y": "old"}, id="e1")
    doc.add(e)
    op = new_operation(
        command_id="x", source="user", actor=_actor().to_dict(),
        patches=[
            Patch("t", "e1", "x", "set", 1, 2),
            Patch("t", "e1", "y", "set", "old", "new"),
        ],
        reverse_patches=[],
    )
    changes = affected_entities(op, doc)
    assert len(changes) == 1
    c = changes[0]
    assert c.change_kind == "updated"
    assert c.changed_fields == ("x", "y")   # sorted
    assert len(c.patches) == 2
    assert c.snapshot is not None
    assert c.snapshot["id"] == "e1"


def test_aggregate_created():
    doc = _doc()
    e = Entity(type="t", data={"x": 1}, id="e1")
    doc.add(e)
    op = new_operation(
        command_id="entity.add", source="user", actor=_actor().to_dict(),
        patches=[Patch("t", "e1", ENTITY_META_FIELD, "create", None, e.to_dict())],
        reverse_patches=[],
    )
    changes = affected_entities(op, doc)
    assert len(changes) == 1
    assert changes[0].change_kind == "created"


def test_aggregate_soft_deleted():
    doc = _doc()
    e = Entity(type="t", data={}, id="e1")
    doc.add(e)
    before_snap = e.to_dict()
    e.delete(expected_version=1, actor=_actor())
    op = new_operation(
        command_id="entity.delete", source="user", actor=_actor().to_dict(),
        patches=[Patch("t", "e1", ENTITY_META_FIELD, "soft_delete",
                       before_snap, e.to_dict())],
        reverse_patches=[],
    )
    changes = affected_entities(op, doc)
    assert changes[0].change_kind == "soft_deleted"


def test_aggregate_restored():
    doc = _doc()
    e = Entity(type="t", data={}, id="e1")
    doc.add(e)
    e.delete(expected_version=1, actor=_actor())
    before_snap = e.to_dict()
    e.restore(expected_version=e.version, actor=_actor())
    op = new_operation(
        command_id="entity.restore", source="user", actor=_actor().to_dict(),
        patches=[Patch("t", "e1", ENTITY_META_FIELD, "restore",
                       before_snap, e.to_dict())],
        reverse_patches=[],
    )
    changes = affected_entities(op, doc)
    assert changes[0].change_kind == "restored"


def test_aggregate_hard_removed():
    doc = _doc()
    e = Entity(type="t", data={"x": 1}, id="e1")
    doc.add(e)
    snap = e.to_dict()
    doc.hard_remove("t", "e1")
    op = new_operation(
        command_id="entity.hard_remove", source="user", actor=_actor().to_dict(),
        patches=[Patch("t", "e1", ENTITY_META_FIELD, "delete", snap, None)],
        reverse_patches=[],
    )
    changes = affected_entities(op, doc)
    assert changes[0].change_kind == "hard_removed"
    assert changes[0].snapshot is None   # hard_removed snapshot 必 None


def test_aggregate_multi_entity_grouped():
    """一个 op 影响多 entity, 应产生多个 EntityChange."""
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    doc.add(Entity(type="t", data={"x": 1}, id="e2"))
    op = new_operation(
        command_id="x", source="user", actor=_actor().to_dict(),
        patches=[
            Patch("t", "e1", "x", "set", 1, 2),
            Patch("t", "e2", "x", "set", 1, 3),
        ],
        reverse_patches=[],
    )
    changes = affected_entities(op, doc)
    assert len(changes) == 2
    assert {c.entity_id for c in changes} == {"e1", "e2"}


def test_aggregate_sorted_order():
    """返回按 (entity_type, entity_id) sorted."""
    doc = _doc()
    for et, eid in [("zebra", "a"), ("alpha", "b"), ("mid", "c")]:
        doc.add(Entity(type=et, data={}, id=eid))
    op = new_operation(
        command_id="x", source="user", actor=_actor().to_dict(),
        patches=[
            Patch("zebra", "a", "k", "set", 0, 1),
            Patch("alpha", "b", "k", "set", 0, 1),
            Patch("mid", "c", "k", "set", 0, 1),
        ],
        reverse_patches=[],
    )
    changes = affected_entities(op, doc)
    types = [c.entity_type for c in changes]
    assert types == sorted(types)


def test_aggregate_create_overrides_update():
    """同 entity 同时有 create 跟 set patch → 视为 created."""
    doc = _doc()
    e = Entity(type="t", data={"x": 1}, id="e1")
    doc.add(e)
    op = new_operation(
        command_id="x", source="user", actor=_actor().to_dict(),
        patches=[
            Patch("t", "e1", ENTITY_META_FIELD, "create", None, e.to_dict()),
            Patch("t", "e1", "x", "set", None, 1),
        ],
        reverse_patches=[],
    )
    changes = affected_entities(op, doc)
    assert changes[0].change_kind == "created"


def test_aggregate_empty_patches():
    doc = _doc()
    op = new_operation(
        command_id="x", source="user", actor=_actor().to_dict(),
        patches=[], reverse_patches=[],
    )
    assert affected_entities(op, doc) == []


def test_aggregate_changed_fields_dedup():
    """同字段多次 patch 在 changed_fields 里应去重."""
    doc = _doc()
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    op = new_operation(
        command_id="x", source="user", actor=_actor().to_dict(),
        patches=[
            Patch("t", "e1", "x", "set", 1, 2),
            Patch("t", "e1", "x", "set", 2, 3),   # 同字段 x 两次
        ],
        reverse_patches=[],
    )
    changes = affected_entities(op, doc)
    assert changes[0].changed_fields == ("x",)


# ==============================================================
# to_broadcast_payload
# ==============================================================

def test_payload_has_required_keys():
    doc = _doc()
    doc.add(Entity(type="t", data={}, id="e1"))
    op = new_operation(
        command_id="x", source="user", actor=_actor().to_dict(),
        patches=[Patch("t", "e1", "k", "set", None, 1)],
        reverse_patches=[],
    )
    changes = affected_entities(op, doc)
    payload = to_broadcast_payload(op, changes, doc_id="doc-abc")
    assert payload["type"] == "entity_change"
    assert payload["doc_id"] == "doc-abc"
    assert payload["op_id"] == op.id
    assert payload["trace_id"] == op.trace_id
    assert payload["source"] == "user"
    assert payload["command_id"] == "x"
    assert len(payload["changes"]) == 1


def test_payload_actor_is_copy():
    """payload 里的 actor 改了不影响 op 内部."""
    doc = _doc()
    doc.add(Entity(type="t", data={}, id="e1"))
    op = new_operation(
        command_id="x", source="user", actor=_actor().to_dict(),
        patches=[Patch("t", "e1", "k", "set", None, 1)],
        reverse_patches=[],
    )
    payload = to_broadcast_payload(op, affected_entities(op, doc))
    payload["actor"]["user_id"] = "TAMPERED"
    assert op.actor["user_id"] == "u1"


def test_payload_no_doc_id():
    """不传 doc_id 时 payload['doc_id'] = None."""
    doc = _doc()
    op = new_operation(
        command_id="x", source="user", actor=_actor().to_dict(),
        patches=[], reverse_patches=[],
    )
    payload = to_broadcast_payload(op, [])
    assert payload["doc_id"] is None


# ==============================================================
# audit_gates: 跑全部 + 单个 + leaf
# ==============================================================


@pytest.mark.parametrize(
    ("source", "marker"),
    (
        ("import history_authority as authority\n", ":broad-import:"),
        (
            "from package import history_authority as authority\n",
            ":broad-from-import:",
        ),
        ("from history_authority import *\n", ":star-import:"),
        (
            "from importlib import import_module as load\n"
            "authority = load('history_' + 'authority')\n",
            ":dynamic-import:",
        ),
        (
            "import sys as runtime\n"
            "loaded = runtime.modules\n"
            "authority = loaded.get('history_' + 'authority')\n",
            ":sys-modules:",
        ),
    ),
)
def test_history_authority_import_census_rejects_broad_access(
    source: str,
    marker: str,
):
    _records, escapes = _history_authority_import_records(
        source,
        filename="escape.py",
    )
    assert any(marker in escape for escape in escapes), escapes


def test_history_authority_import_census_preserves_alias_for_exact_comparison():
    records, escapes = _history_authority_import_records(
        "from history_authority import _history_coordinator_port as open_port\n",
        filename="consumer.py",
    )
    assert records == [("_history_coordinator_port", "open_port", 1)]
    assert escapes == []


def test_history_authority_inventory_reports_wrong_owner_but_skips_tests(tmp_path):
    (tmp_path / "server.py").write_text(
        "from history_authority import _history_coordinator_port\n",
        encoding="utf-8",
    )
    test_source = tmp_path / "sdk" / "test_history_escape.py"
    test_source.parent.mkdir(parents=True)
    test_source.write_text(
        "import history_authority as authority\n",
        encoding="utf-8",
    )
    nested_test = tmp_path / "tests" / "helper.py"
    nested_test.parent.mkdir(parents=True)
    nested_test.write_text(
        "import history_authority as authority\n",
        encoding="utf-8",
    )

    inventory, escapes = _history_authority_production_inventory(tmp_path)
    assert inventory == {
        "server.py": (("_history_coordinator_port", ""),),
    }
    assert escapes == []


def test_history_authority_public_surface_guard_rejects_raw_and_private_seams():
    history_source = """
class HistoryTransaction:
    pass
class _HistoryReplayPort:
    pass
_HISTORY_REPLAY_CONSUMER_CAPABILITY = object()
def _history_replay_port():
    pass
def current_history_transaction():
    pass
__all__ = ["current_history_transaction", "_history_replay_port"]
"""
    sdk_source = '__all__ = ["HistoryTransaction", "_history_replay_port"]\n'
    public_lock = json.dumps({
        "py": {"_HISTORY_REPLAY_CONSUMER_CAPABILITY": []},
    })
    violations = _history_authority_public_surface_violations(
        history_source,
        sdk_source,
        public_lock,
    )
    assert any(
        value.startswith("raw-transaction-lookup-defined:")
        for value in violations
    )
    assert any(value.startswith("history-__all__:") for value in violations)
    assert any(value.startswith("history-private-exports:") for value in violations)
    assert any(value.startswith("sdk-__all__:") for value in violations)
    assert any(value.startswith("public-lock:") for value in violations)


def test_history_authority_public_surface_guard_accepts_private_raw_type_only():
    history_source = """
class HistoryTransaction:
    pass
class _HistoryReplayPort:
    pass
_HISTORY_REPLAY_CONSUMER_CAPABILITY = object()
def _history_replay_port():
    pass
__all__ = ["HistoryAuthority"]
"""
    assert _history_authority_public_surface_violations(
        history_source,
        '__all__ = ["Document"]\n',
        json.dumps({"py": {}}),
    ) == []


def test_commit_02_history_authority_consumer_gate_passes():
    result = check_commit_02_production_surfaces_use_coordinator()
    assert result.status == "PASS", (result.msg, result.evidence)

def test_run_all_gates_pass():
    results = run_all()
    failed = [r for r in results if r.status == "FAIL"]
    if failed:
        msgs = "\n".join(f"  {r.gate_id}: {r.msg}" for r in failed)
        pytest.fail(f"以下 gate FAIL:\n{msgs}")
    # 全部 PASS
    assert all(r.status == "PASS" for r in results)


def test_run_one_gate():
    r = run_one("GATE-OP-02")
    assert r.gate_id == "GATE-OP-02"
    assert r.status == "PASS"


def test_run_one_unknown_gate():
    r = run_one("GATE-NOPE-99")
    assert r.status == "SKIP"
    assert "unknown" in r.msg.lower()


def test_run_leaf_gates():
    """跑某叶子的全部 gate."""
    results = run_leaf("DOC")
    assert len(results) >= 2     # DOC-01, DOC-02
    assert all(r.gate_id.startswith("GATE-DOC-") for r in results)


def test_db_05_business_snapshot_gate_passes():
    result = check_db_05_business_snapshot_root_and_restore()
    assert result.status == "PASS", result.msg


def test_db_08_sqlite_legacy_import_schema_v3_gate_passes():
    result = check_db_08_sqlite_legacy_import_schema_v3()
    assert result.status == "PASS", result.msg


def test_db_10_sqlite_legacy_catalog_historical_seal_v4_gate_passes():
    result = check_db_10_sqlite_legacy_catalog_historical_seal_v4()
    assert result.status == "PASS", result.msg


def test_db_12_sqlite_repository_exact_proof_v5_gate_passes():
    result = check_db_12_sqlite_repository_exact_proof_v5()
    assert result.status == "PASS", result.msg


def test_db_19_sqlite_mount_chronology_state_closure_v11_gate_passes():
    result = check_db_19_sqlite_mount_chronology_state_closure_v11()
    assert result.status == "PASS", result.msg


def test_db_20_sqlite_cutover_install_closure_v12_gate_passes():
    result = check_db_20_sqlite_cutover_install_closure_v12()
    assert result.status == "PASS", result.msg


def test_commit_11_canonical_cutover_asset_members_and_phase_head_gate_passes():
    result = check_commit_11_canonical_cutover_asset_members_and_phase_head()
    assert result.status == "PASS", result.msg


def test_commit_12_sqlite_cutover_install_fixed_writer_gate_passes():
    result = check_commit_12_sqlite_cutover_install_fixed_writer()
    assert result.status == "PASS", result.msg


def test_commit_13_sqlite_cutover_plan_free_durable_audit_gate_passes():
    result = check_commit_13_sqlite_cutover_plan_free_durable_audit()
    assert result.status == "PASS", result.msg


def test_commit_14_sqlite_cutover_phase_one_head_finalizer_gate_passes():
    result = check_commit_14_sqlite_cutover_phase_one_head_finalizer()
    assert result.status == "PASS", result.msg


def test_commit_15_sqlite_cutover_asset_provider_phase_writer_gate_passes():
    result = check_commit_15_sqlite_cutover_asset_provider_phase_writer()
    assert result.status == "PASS", result.msg


def test_doc_07_safe_entity_file_store_gate_passes():
    result = check_doc_07_safe_entity_file_store()
    assert result.status == "PASS", result.msg


def test_doc_09_validated_head_authority_gate_passes():
    result = check_doc_09_validated_head_authority()
    assert result.status == "PASS", result.msg


def test_doc_09_source_inventory_excludes_virtualenv_and_runtime_trees(tmp_path):
    live = tmp_path / "02-mod" / "05-code" / "live.py"
    dependency = tmp_path / ".venv" / "lib" / "python3.13" / "site-packages" / "dep.py"
    runtime = tmp_path / ".data" / "generated.py"
    for path, source in (
        (live, "VALUE = 1\n"),
        (dependency, "type Alias[T] = T\n"),
        (runtime, "not valid python !!!\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    found = {path.relative_to(tmp_path).as_posix() for path in _iter_auditable_python_sources(tmp_path)}
    assert found == {"02-mod/05-code/live.py"}


def test_project_08_validated_head_consumers_gate_passes():
    result = check_project_08_validated_head_consumers()
    assert result.status == "PASS", result.msg


def test_project_09_strict_catalog_migration_barrier_gate_passes():
    result = check_project_09_strict_catalog_migration_barrier_and_fork_fence()
    assert result.status == "PASS", result.msg


def test_project_10_repository_writer_admission_gate_passes():
    result = check_project_10_repository_writer_admission_and_complete_state()
    assert result.status == "PASS", result.msg


def test_project_11_same_barrier_repository_proof_gate_passes():
    result = check_project_11_same_barrier_exact_repository_proof()
    assert result.status == "PASS", result.msg


def test_project_13_private_runtime_provenance_gate_passes():
    result = check_project_13_private_business_runtime_production_zero()
    assert result.status == "PASS", result.msg


def test_project_08_raw_head_ast_tracks_aliases_and_allows_trusted_reader():
    safe = """
def metadata(root):
    head_path = root / "document.json"
    return read_document_metadata(head_path)
"""
    unsafe = """
import json

def metadata(root):
    head_path = root / "document.json"
    alias = head_path
    payload = alias.read_text(encoding="utf-8")
    return json.loads(payload)
"""
    unsafe_open = """
def metadata(root):
    document_path = root / "document.json"
    with open(document_path, "rb") as source:
        return source.read()
"""
    attribute_read = """
import json

def metadata(ctx):
    return json.loads(ctx.doc_path.read_text(encoding="utf-8"))
"""
    durable_helper = """
def metadata(topology, root):
    return topology.read_durable_source_file(root, "document.json")
"""
    replace_and_dump = """
import json
import os

def replace(root, temporary):
    target = root / "document.json"
    os.replace(temporary, target)

def dump(root, payload):
    target = root / "document.json"
    with target.open("w", encoding="utf-8") as output:
        json.dump(payload, output)
"""
    local_helper = """
import json

def read_raw(path):
    payload = path.read_text(encoding="utf-8")
    return json.loads(payload)

def metadata(root):
    return read_raw(root / "document.json")
"""
    unknown_result = """
def decorate(value):
    return value

def metadata(root):
    selected = decorate(root / "document.json")
    return selected.read_bytes()
"""
    unrelated_documentation = """
def read_documentation(documentation_path):
    return documentation_path.read_text(encoding="utf-8")
"""
    assert _find_raw_document_head_accesses(safe) == []
    alias_violations = _find_raw_document_head_accesses(unsafe)
    assert any("read_text" in violation for violation in alias_violations)
    assert any("json.loads" in violation for violation in alias_violations)
    assert any(
        violation.endswith(":open")
        for violation in _find_raw_document_head_accesses(unsafe_open)
    )
    attribute_violations = _find_raw_document_head_accesses(attribute_read)
    assert any("read_text" in violation for violation in attribute_violations)
    assert any("json.loads" in violation for violation in attribute_violations)
    assert any(
        "read_durable_source_file" in violation
        for violation in _find_raw_document_head_accesses(durable_helper)
    )
    mutation_violations = _find_raw_document_head_accesses(replace_and_dump)
    assert any("os.replace" in violation for violation in mutation_violations)
    assert any("json.dump" in violation for violation in mutation_violations)
    helper_violations = _find_raw_document_head_accesses(local_helper)
    assert any(
        ":read_raw:read_text" in violation
        for violation in helper_violations
    )
    assert any(
        ":read_raw:json.loads" in violation
        for violation in helper_violations
    )
    assert any(
        ":metadata:read_bytes" in violation
        for violation in _find_raw_document_head_accesses(unknown_result)
    )
    assert _find_raw_document_head_accesses(unrelated_documentation) == []


def test_project_08_head_guard_ignores_unexecuted_nested_scope_decoys():
    requirements = {
        "_assert_head_identity", "verify_source",
        "copy_durable_source", "rename",
    }
    decoys = [ast.parse(source).body[0] for source in (
        """
def duplicate():
    with open_document_head(source):
        def never_called():
            _assert_head_identity()
            verify_source()
            copy_durable_source()
            rename()
    rename_outside_the_guard()
""",
        """
def duplicate():
    with open_document_head(source):
        class NeverConstructed:
            def run(self):
                _assert_head_identity()
                verify_source()
                copy_durable_source()
                rename()
    rename_outside_the_guard()
""",
        """
def duplicate():
    with open_document_head(source):
        never_called = lambda: (
            _assert_head_identity(), verify_source(),
            copy_durable_source(), rename(),
        )
    rename_outside_the_guard()
""",
    )]
    direct = ast.parse("""
def duplicate():
    with open_document_head(source):
        _assert_head_identity()
        verify_source()
        copy_durable_source()
        rename()
""").body[0]
    assert all(
        not _gate_head_guard_spans(decoy, requirements)
        for decoy in decoys
    )
    assert _gate_head_guard_spans(direct, requirements)


def test_doc_09_private_rewrite_authority_ast_rejects_import_and_dynamic_lookup():
    imported = """
from document import (
    _TOPOLOGY_STAGING_METADATA_CAPABILITY,
    _rewrite_document_metadata_for_topology_staging,
)
"""
    dynamic = """
import document

rewrite = getattr(document, "_rewrite_document_metadata_for_topology_staging")
"""
    assert _find_private_topology_rewrite_uses(imported)
    assert any(
        "_rewrite_document_metadata_for_topology_staging" in use
        for use in _find_private_topology_rewrite_uses(dynamic)
    )


def test_save_policy_gate_rejects_missing_legacy_and_dynamic_values(tmp_path):
    mod = tmp_path / "02-mod" / "05-code"
    mod.mkdir(parents=True)
    source = mod / "document_register.py"

    source.write_text("def register(registry):\n    pass\n")
    assert check_doc_05_save_policy_declared(tmp_path).status == "FAIL"

    source.write_text("def register(registry):\n    registry.set_save_policy('legacy')\n")
    assert check_doc_05_save_policy_declared(tmp_path).status == "FAIL"

    source.write_text("def register(registry):\n    registry.set_save_policy(POLICY)\n")
    assert check_doc_05_save_policy_declared(tmp_path).status == "FAIL"

    source.write_text("def register(registry):\n    registry.set_save_policy('auto_commit')\n")
    assert check_doc_05_save_policy_declared(tmp_path).status == "PASS"


def test_audit_surface_scope_uses_only_the_declared_profile(tmp_path):
    _write_surface_profile(
        tmp_path,
        primary="cli",
        surfaces=["cli"],
        entrypoints={"cli": "scripts/ze_command.py"},
    )
    headless = load_audit_surface_scope(tmp_path)
    assert headless.surfaces == frozenset(("cli",))
    assert headless.web_root is None
    assert headless.server_file is None
    assert not headless.requires_http_server

    _write_surface_profile(
        tmp_path,
        primary="api",
        surfaces=["api"],
        entrypoints={"api": "service.py"},
        api={"server": "service.py"},
    )
    api = load_audit_surface_scope(tmp_path)
    assert api.server_file == tmp_path / "service.py"
    assert api.web_root is None
    assert api.requires_http_server

    _write_surface_profile(
        tmp_path,
        primary="web",
        surfaces=["web", "web"],
        entrypoints={"web": "webapp"},
        web={"dir": "webapp"},
    )
    with pytest.raises(ValueError, match="duplicates"):
        load_audit_surface_scope(tmp_path)


def test_surface_aware_gates_do_not_require_web_for_headless_cli(tmp_path):
    _write_surface_profile(
        tmp_path,
        primary="cli",
        surfaces=["cli"],
        entrypoints={"cli": "scripts/ze_command.py"},
    )
    assert not (tmp_path / "server.py").exists()
    assert not (tmp_path / "webapp").exists()
    assert check_project_05_stream_generation_cursor_fence(tmp_path).status == "PASS"
    assert check_rt_07_entity_status_snapshot_replaces(tmp_path).status == "PASS"
    assert check_auth_09_operation_manual_trust_boundary(tmp_path).status == "PASS"


def test_surface_aware_api_gate_requires_server_but_not_webapp(tmp_path):
    _write_surface_profile(
        tmp_path,
        primary="api",
        surfaces=["api"],
        entrypoints={"api": "server.py"},
        api={"server": "server.py"},
    )
    (tmp_path / "server.py").write_text(
        "\n".join((
            "event_cursor", "event_epoch", "project_generation",
            "resync_required", "access_revoked", "replay_gap",
            "wait_until_delivered", "delivery_barrier_timeout",
            "_assert_stream_access_locked", "cursor_before == cursor_after",
            'stat_snap["replace"] = True', "entity_status_snapshot()",
            '@app.post("/api/asset-tickets")', "_scan_durable_asset_refs",
            "_durable_asset_access", "_media_ticket_subject_allowed",
            "Depends(_get_header_cookie_actor)", '"private, no-store, max-age=0"',
        )),
        encoding="utf-8",
    )
    bootstrap = tmp_path / "sdk" / "bootstrap.py"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("MediaTicketSigner(tool_id=self.tool_id)", encoding="utf-8")
    ticket_source = (
        tmp_path / "01-core" / "04-user-facing" / "03-authentication"
        / "media_ticket.py"
    )
    ticket_source.parent.mkdir(parents=True)
    ticket_source.write_text(
        "_load_master_key\nze.media-ticket.signing.v1\n", encoding="utf-8",
    )
    assert not (tmp_path / "webapp").exists()
    assert check_project_05_stream_generation_cursor_fence(tmp_path).status == "PASS"
    assert check_rt_07_entity_status_snapshot_replaces(tmp_path).status == "PASS"
    assert check_ast_07_media_ticket_private_delivery(tmp_path).status == "PASS"


def test_surface_aware_web_gate_fails_when_declared_ts_is_missing(tmp_path):
    _write_surface_profile(
        tmp_path,
        primary="web",
        surfaces=["web"],
        entrypoints={"web": "webapp"},
        web={"dir": "webapp"},
    )
    (tmp_path / "server.py").write_text(
        "\n".join((
            "event_cursor", "event_epoch", "project_generation",
            "resync_required", "access_revoked", "replay_gap",
            "wait_until_delivered", "delivery_barrier_timeout",
            "_assert_stream_access_locked", "cursor_before == cursor_after",
        )),
        encoding="utf-8",
    )
    result = check_project_05_stream_generation_cursor_fence(tmp_path)
    assert result.status == "FAIL"
    assert "missing client source" in result.msg


def test_gates_registry_complete():
    """GATES 应该跟 AUDIT-GATES.md §5 列的 gate 数一致."""
    expected = {
        "GATE-DOC-01", "GATE-DOC-02", "GATE-DOC-05", "GATE-DOC-06", "GATE-DOC-07",
        "GATE-DOC-08", "GATE-DOC-09", "GATE-DOC-10",
        "GATE-ENT-01", "GATE-ENT-02", "GATE-ENT-03",
        "GATE-AST-01", "GATE-AST-02", "GATE-AST-04", "GATE-AST-05", "GATE-AST-06", "GATE-AST-07",
        "GATE-OP-02", "GATE-OP-03", "GATE-OP-04", "GATE-OP-06", "GATE-OP-07", "GATE-OP-08",
        "GATE-CMD-01", "GATE-CMD-03", "GATE-CMD-04", "GATE-CMD-05", "GATE-CMD-07",
        "GATE-CC-04",
        "GATE-CONF-01",
        "GATE-FT-01", "GATE-FT-02", "GATE-FT-03",
        "GATE-FT-04", "GATE-FT-05", "GATE-FT-06", "GATE-FT-07",
        "GATE-AI-01", "GATE-AI-04", "GATE-AI-05", "GATE-AI-06",
        "GATE-CAP-01", "GATE-CAP-02", "GATE-CAP-03",
        "GATE-RB-01", "GATE-RB-02", "GATE-RB-03", "GATE-RB-04", "GATE-RB-05", "GATE-RB-06",
        "GATE-UNDO-01", "GATE-UNDO-02", "GATE-UNDO-03",
        "GATE-UNDO-04", "GATE-UNDO-05",
        "GATE-RT-01", "GATE-RT-02", "GATE-RT-03",
        "GATE-RT-04", "GATE-RT-05", "GATE-RT-06", "GATE-RT-07",
        "GATE-COMMIT-01", "GATE-COMMIT-02", "GATE-COMMIT-03", "GATE-COMMIT-04",
        "GATE-COMMIT-05", "GATE-COMMIT-06", "GATE-COMMIT-07", "GATE-COMMIT-08",
        "GATE-COMMIT-09", "GATE-COMMIT-10", "GATE-COMMIT-11", "GATE-COMMIT-12",
        "GATE-COMMIT-13", "GATE-COMMIT-14", "GATE-COMMIT-15",
        "GATE-DB-01", "GATE-DB-02", "GATE-DB-03", "GATE-DB-04", "GATE-DB-05",
        "GATE-DB-06", "GATE-DB-07", "GATE-DB-08", "GATE-DB-09", "GATE-DB-10",
        "GATE-DB-11", "GATE-DB-12", "GATE-DB-13", "GATE-DB-14", "GATE-DB-15",
        "GATE-DB-16", "GATE-DB-17", "GATE-DB-18", "GATE-DB-19", "GATE-DB-20",
        "GATE-DB-21",
        "GATE-PROJECT-01", "GATE-PROJECT-02", "GATE-PROJECT-03",
        "GATE-PROJECT-04", "GATE-PROJECT-05", "GATE-PROJECT-06", "GATE-PROJECT-07",
        "GATE-PROJECT-08", "GATE-PROJECT-09", "GATE-PROJECT-10",
        "GATE-PROJECT-11", "GATE-PROJECT-12", "GATE-PROJECT-13",
        "GATE-AUTH-01", "GATE-AUTH-02", "GATE-AUTH-03",
        "GATE-AUTH-04", "GATE-AUTH-05", "GATE-AUTH-06", "GATE-AUTH-07", "GATE-AUTH-08",
        "GATE-AUTH-09", "GATE-AUTH-10",
        "GATE-PROTO-01", "GATE-PROTO-02", "GATE-PROTO-03", "GATE-PROTO-04",
        "GATE-PIPE-04", "GATE-PIPE-05", "GATE-PIPE-06", "GATE-PIPE-07", "GATE-PIPE-08",
        "GATE-PIPE-09",
        "GATE-TYPE-01", "GATE-TYPE-02", "GATE-TYPE-03",
        "GATE-TYPE-04", "GATE-TYPE-05", "GATE-TYPE-06", "GATE-TYPE-08", "GATE-TYPE-10",
        "GATE-TYPE-11",
    }
    assert set(GATES.keys()) == expected


def test_gate_registry_and_human_counts_stay_mechanically_consistent():
    """Runner、完整注册表与 README 摘要不能再靠手算各自漂移。"""
    protocol_root = Path(__file__).resolve().parent
    audit_text = (protocol_root / "AUDIT-GATES.md").read_text(encoding="utf-8")
    readme_text = (protocol_root / "README.md").read_text(encoding="utf-8")
    registered = set(re.findall(r"GATE-[A-Z]+-\d{2}", audit_text))
    externally_enforced = {"GATE-TYPE-07", "GATE-TYPE-09"}
    executable = set(GATES).union(externally_enforced)
    pending = registered.difference(executable)

    assert len(GATES) == 140
    assert len(registered) == 162
    assert executable.issubset(registered)
    assert len(executable) == 142
    assert len(pending) == 20

    registered_by_leaf = Counter(gate_id.split("-")[1] for gate_id in registered)
    runner_by_leaf = Counter(gate_id.split("-")[1] for gate_id in GATES)
    assert registered_by_leaf["COMMIT"] == runner_by_leaf["COMMIT"] == 15
    assert registered_by_leaf["DB"] == runner_by_leaf["DB"] == 21
    assert registered_by_leaf["PROJECT"] == runner_by_leaf["PROJECT"] == 13

    for required in (
        "注册 162 个 gate",
        "140 个由 audit runner 实施",
        "合计 142 个可执行协议 gate",
        "剩余待补 20 个",
        "COMMIT×15",
        "DB×21",
        "PROJECT×13",
    ):
        assert required in readme_text or required in audit_text

    assert re.findall(
        r"audit_gates\.py\]\([^\n]+\) \| (\d+) 个 runtime",
        readme_text,
    ) == ["140"]
    assert re.findall(
        r"# 总计: (\d+); PASS (\d+), FAIL 0, SKIP 0",
        readme_text,
    ) == [("140", "140")]


def test_pipeline_delegated_mutation_gate_is_dormant_and_detects_a_caller(
    tmp_path,
):
    assert check_pipe_09_delegated_mutation_dormant().status == "PASS"

    module = tmp_path / "01-core" / "_protocol" / "pipeline_delegated_mutation.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "def prepare_dormant_pipeline_delegated_mutation_request():\n"
        "    return None\n"
        "def consume_dormant_pipeline_delegated_mutation_request():\n"
        "    return None\n",
        encoding="utf-8",
    )
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "__init__.py").write_text("", encoding="utf-8")
    (module.parent / "PUBLIC_SURFACE.lock.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (sdk / "caller.py").write_text(
        "from pipeline_delegated_mutation import "
        "prepare_dormant_pipeline_delegated_mutation_request\n",
        encoding="utf-8",
    )

    violations = _pipeline_delegated_dormant_violations(tmp_path, module)
    assert any("production import: sdk/caller.py" in value for value in violations)


def test_pipeline_delegated_mutation_gate_detects_relative_dynamic_and_effects(
    tmp_path,
):
    module = tmp_path / "01-core" / "_protocol" / "pipeline_delegated_mutation.py"
    module.parent.mkdir(parents=True)
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "__init__.py").write_text("", encoding="utf-8")
    (module.parent / "PUBLIC_SURFACE.lock.json").write_text(
        "{}",
        encoding="utf-8",
    )

    module.write_text(
        "def prepare_dormant_pipeline_delegated_mutation_request():\n"
        "    store.commit()\n"
        "def consume_dormant_pipeline_delegated_mutation_request():\n"
        "    return None\n",
        encoding="utf-8",
    )
    (sdk / "relative_caller.py").write_text(
        "from . import pipeline_delegated_mutation as delegated\n",
        encoding="utf-8",
    )
    (sdk / "dynamic_caller.py").write_text(
        "import importlib as loader\n"
        "MODULE = 'pipeline_delegated_' + 'mutation'\n"
        "load = loader.import_module\n"
        "delegated = load(MODULE)\n",
        encoding="utf-8",
    )

    violations = _pipeline_delegated_dormant_violations(tmp_path, module)
    assert any(
        "delegated module calls unreviewed surface: store.commit" in value
        for value in violations
    )
    assert any(
        "production import: sdk/relative_caller.py" in value
        and "from . import pipeline_delegated_mutation" in value
        for value in violations
    )
    assert any(
        "production import: sdk/dynamic_caller.py" in value
        and "dynamic import via load" in value
        for value in violations
    )
