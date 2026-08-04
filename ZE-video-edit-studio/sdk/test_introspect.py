"""sdk/test_introspect.py — AI 最低接口 reader 单测.

临时 ZE_DATA_DIR 起 Repository + seed demo project,逐 reader 断言 shape;
重点:Entity.Select 只读(不改 op_log 长度 / doc.version)。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sdk import (  # noqa: E402
    Actor, CommandRegistry, CommandSpec, Document, OpLog, new_operation,
    describe_context, list_commands, describe_command, list_entities,
    entity_select, list_editors, list_pipelines, list_tasks, query_oplog, read_manual,
)


def _seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))
    from sdk.bootstrap import bootstrap
    repo = bootstrap()
    pid = repo.create_project(name="T", owner="u1", seed=True)
    return repo, repo.get_or_load(pid)


def test_describe_context_shape(tmp_path, monkeypatch):
    repo, ctx = _seeded(tmp_path, monkeypatch)
    d = describe_context(ctx, introspect=repo.introspect, config=repo.config)
    for k in ("document", "entity_types", "editors", "commands", "guided_tours",
              "pipelines", "tasks",
              "operation_log", "ai_flow", "manual_available", "settings"):
        assert k in d, f"Context.Describe 缺 key: {k}"
    # guided_tours(系统 3a):无 U0 实例时从 ai_flow/说明书回退派生;摘要 shape 固定。
    assert isinstance(d["guided_tours"], list)
    for t in d["guided_tours"]:
        assert {"id", "title", "trigger", "when", "mode", "step_count"} <= set(t)
    assert d["document"]["id"]
    assert any(c["id"] == "entity.add" for c in d["commands"])  # builtin,任何 mod 都有
    # 以下 mod-agnostic(本文件随 core 复制到每个 skill,不能耦合具体 mod 的编辑器/ai_flow):
    assert isinstance(d["editors"], list)
    for e in d["editors"]:
        assert {"slot", "editor_class", "bind"} <= set(e)
    assert isinstance(d["ai_flow"], dict)         # 有 ai_flow_register 则非空,无则 {}
    assert isinstance(d["pipelines"], list)
    assert isinstance(d["manual_available"], bool)


def test_command_list_describe(tmp_path, monkeypatch):
    repo, ctx = _seeded(tmp_path, monkeypatch)
    cmds = list_commands(ctx.cmd_reg)
    assert any(c["id"] == "entity.add" for c in cmds)
    # 确认门控字段:brief(list)即带 requires_confirm,AI 提前知是否两段式
    assert all("requires_confirm" in c for c in cmds)
    one = describe_command(ctx.cmd_reg, "entity.add")
    assert one["id"] == "entity.add"
    assert "params_schema" in one and "has_poll" in one
    # describe 额外暴露 confirm_template;builtin 默认非确认
    assert one["requires_confirm"] is False and "confirm_template" in one


def test_command_introspection_uses_detached_registration_time_declaration():
    registry = CommandRegistry()
    original_poll = lambda args: True
    spec = CommandSpec(
        id="sealed.command",
        label="Original label",
        kind="stage",
        phase=2,
        touches=["entry"],
        sources_allowed=("user",),
        requires_confirm=False,
        confirm_template="Original {value}",
        description="Original description",
        params_schema={"properties": {"value": {"type": "integer"}}},
        outputs_schema={"properties": {"result": {"type": "integer"}}},
        ui_ports={"inputs": [{"id": "original-input"}]},
        poll=original_poll,
        execute=lambda **kwargs: new_operation(
            command_id="sealed.command",
            source=kwargs["source"],
            actor=kwargs["actor"].to_dict(),
            patches=(),
            reverse_patches=(),
        ),
    )
    registry.register(spec)

    # Every public declaration field remains mutable for legacy callers, but
    # none of these aliases may change production introspection or dispatch.
    spec.label = "Forged label"
    spec.touches.append("forged")
    spec.sources_allowed = ("ai",)
    spec.requires_confirm = True
    spec.confirm_template = "Forged"
    spec.description = "Forged description"
    spec.params_schema["properties"]["value"]["type"] = "string"
    spec.outputs_schema["properties"]["result"]["type"] = "string"
    spec.ui_ports["inputs"][0]["id"] = "forged-input"
    spec.poll = lambda args: False

    listed = next(
        item for item in list_commands(registry)
        if item["id"] == "sealed.command"
    )
    described = describe_command(registry, "sealed.command")
    assert listed["label"] == "Original label"
    assert listed["touches"] == ["entry"]
    assert listed["sources_allowed"] == ["user"]
    assert listed["requires_confirm"] is False
    assert described["confirm_template"] == "Original {value}"
    assert described["description"] == "Original description"
    assert described["params_schema"]["properties"]["value"]["type"] == "integer"
    assert described["outputs_schema"]["properties"]["result"]["type"] == "integer"
    assert described["ui_ports"]["inputs"][0]["id"] == "original-input"
    assert described["has_poll"] is True
    assert registry.can("sealed.command", {}) is True

    # Returned projections are disposable values, never aliases to the seal.
    described["params_schema"]["properties"]["value"]["type"] = "forged-again"
    described["ui_ports"]["inputs"][0]["id"] = "forged-again"
    next_description = describe_command(registry, "sealed.command")
    assert next_description["params_schema"]["properties"]["value"]["type"] == "integer"
    assert next_description["ui_ports"]["inputs"][0]["id"] == "original-input"

    result = registry.dispatch(
        "sealed.command",
        {},
        actor=Actor(user_id="u1"),
        source="user",
        doc=Document(schema_version="1.0", collaboration_mode="with_save"),
    )
    assert result.command_id == "sealed.command"


def test_command_introspection_keeps_narrow_fake_registry_fallback():
    fake_spec = SimpleNamespace(
        id="fake.command",
        label="Fake",
        kind="native",
        phase=1,
        touches=["entry"],
        sources_allowed=("user",),
        requires_confirm=False,
        poll=lambda args: True,
    )

    class FakeRegistry:
        def list(self):
            return [fake_spec]

        def get(self, command_id):
            assert command_id == fake_spec.id
            return fake_spec

    assert list_commands(FakeRegistry())[0]["id"] == "fake.command"
    assert describe_command(FakeRegistry(), "fake.command")["has_poll"] is True


def test_editors_reflect_mirror(tmp_path, monkeypatch):
    repo, ctx = _seeded(tmp_path, monkeypatch)
    eds = list_editors(repo.introspect, ctx.cmd_reg)
    # mod-agnostic:不耦合具体编辑器类(随 core 复制到各 skill);只校结构 + core 计算字段。
    for e in eds:
        assert {"slot", "editor_class", "bind", "needs_renderer", "runnable_commands"} <= set(e)
        assert isinstance(e["needs_renderer"], bool)        # scene3d/chart 才 True,core 计算
        assert isinstance(e["runnable_commands"], list)     # = touches ∩ bind,core 计算


def test_pipeline_static_meta_reflects_mirror(tmp_path, monkeypatch):
    repo, _ctx = _seeded(tmp_path, monkeypatch)
    pipes = list_pipelines(repo.introspect)
    assert isinstance(pipes, list)
    for p in pipes:
        assert "id" in p
        assert isinstance(p.get("stages", []), list)


def test_entity_select_is_readonly(tmp_path, monkeypatch):
    repo, ctx = _seeded(tmp_path, monkeypatch)
    before_v, before_ops = ctx.doc.version, len(ctx.op_log)
    r = entity_select(ctx.doc, "shot", "nonexistent-id")
    assert r["exists"] is False
    assert ctx.doc.version == before_v
    assert len(ctx.op_log) == before_ops          # 只读:不落 op


def test_entities_light_list(tmp_path, monkeypatch):
    repo, ctx = _seeded(tmp_path, monkeypatch)
    out = list_entities(ctx.doc)
    assert isinstance(out, dict)
    for rows in out.values():
        for r in rows:
            assert set(r.keys()) == {"id", "name", "version", "deleted"}


def test_oplog_query_filter(tmp_path, monkeypatch):
    repo, ctx = _seeded(tmp_path, monkeypatch)
    q = query_oplog(ctx.op_log, source="ai")
    assert "entries" in q and q["verified"] is True
    assert q["count"] <= q["total"]


def test_oplog_query_redacts_pipeline_authority_from_every_public_reader():
    secret = "private-work-list-material"

    class CopyBomb:
        def __deepcopy__(self, _memo):
            raise AssertionError("redacted authority material must not be copied")

    row = {
        "op": {
            "id": "1" * 32,
            "command_id": "pipeline.start",
            "pipeline": {
                "schema": "ze.pipeline-start.v1",
                "work_list": {"prompt": secret, "private": CopyBomb()},
            },
        },
        "prev_hash": "0" * 64,
        "self_hash": "2" * 64,
    }

    class FakeLog:
        def entries(self):
            return [row]

        def by_entity(self, *_args):
            return [row]

        def by_source(self, *_args):
            return [row]

        def verify_chain(self):
            return True

        def __len__(self):
            return 1

    for query in (
        query_oplog(FakeLog()),
        query_oplog(FakeLog(), source="user"),
        query_oplog(FakeLog(), entity_type="asset", entity_id="a-1"),
    ):
        assert "pipeline" not in query["entries"][0]["op"]
        assert query["entries"][0]["canonical_body_redacted"] is True
        assert query["entries"][0]["redacted_fields"] == ["op.pipeline"]
        assert secret not in repr(query)
    assert "pipeline" in row["op"], "public projection must not mutate canonical history"


def test_real_oplog_public_query_limits_before_private_authority_copy(monkeypatch):
    log = OpLog()
    operations = []
    for index in range(3):
        operation = new_operation(
            command_id="pipeline.start",
            source="user",
            actor={"user_id": "u-1"},
            patches=[],
            reverse_patches=[],
            pipeline={
                "schema": "ze.pipeline-start.v1",
                "run_id": f"run-{index}",
                "work_list": {"prompt": f"private-{index}"},
            },
        )
        operations.append(operation)
        log.append(operation)

    def forbidden_legacy_copy():
        raise AssertionError("public query must use the pre-redaction OpLog seam")

    monkeypatch.setattr(log, "entries", forbidden_legacy_copy)
    query = query_oplog(log, limit=1)
    assert query["count"] == 1
    assert query["entries"][0]["op"]["id"] == operations[-1].id
    assert query["entries"][0]["canonical_body_redacted"] is True
    assert "private-0" not in repr(query)
    assert "private-1" not in repr(query)
    assert "private-2" not in repr(query)


def test_tasks_and_manual(tmp_path, monkeypatch):
    repo, ctx = _seeded(tmp_path, monkeypatch)
    assert isinstance(list_tasks(ctx.supervisor), list)
    m = read_manual(repo.introspect)             # 有 ai_flow_register 则非空,无则空串
    assert m["format"] == "markdown" and "content" in m
