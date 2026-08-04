"""test_command.py — CommandSpec + CommandRegistry + 9 内置 entity commands.

覆盖:
  - CommandSpec: 构造校验 (kind / sources_allowed)
  - CommandRegistry: register / get / list / has / can / dispatch / 装饰器
  - dispatch 流程: source_allowed 校验 / poll / execute / 返回 Operation / CommandMutation
  - 提交边界: 兼容接收 op_log 但不 append，持久化归 CommitCoordinator
  - execute 必须返回 Operation / CommandMutation; 否则抛 ZeError
  - 9 个内置 entity command 各自 happy path + reverse patch 形态 + OpLog 集成
  - register_builtin_commands 一键注册
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command import CommandMutation, CommandRegistry, CommandSpec
from builtin_commands import register_builtin_commands
from entity import Actor, Entity, ZeError
from operation import (
    ENTITY_META_FIELD,
    Operation,
    OpLog,
    Patch,
    new_operation,
)
from document import Document, _HISTORY_REPLAY_CAPABILITY


# ==============================================================
# helper
# ==============================================================

def _actor():
    return Actor(user_id="u1")


def _apply_patch(doc, patch, *, actor):
    doc.apply_patch_directly(
        patch,
        actor=actor,
        _replay_capability=_HISTORY_REPLAY_CAPABILITY,
    )


def _new_doc():
    return Document(schema_version="1.0.0", collaboration_mode="with_save")


def _dummy_execute(args, actor, source, doc):
    """No-op execute 用于 spec 测试."""
    return new_operation(
        command_id="dummy",
        source=source,
        actor=actor.to_dict(),
        patches=[],
        reverse_patches=[],
    )


# ==============================================================
# CommandSpec 构造
# ==============================================================

def test_command_spec_basic():
    spec = CommandSpec(id="x.y", label="x", kind="native", execute=_dummy_execute)
    assert spec.id == "x.y"
    assert spec.kind == "native"
    assert spec.phase == 1
    assert spec.conflict_strategy == "reject"
    assert spec.scheduling == "immediate"


def test_command_spec_default_sources_allowed():
    spec = CommandSpec(id="x.y", label="x", execute=_dummy_execute)
    assert set(spec.sources_allowed) == {"user", "ai", "sync", "system"}


def test_command_spec_invalid_kind_raises():
    with pytest.raises(ZeError) as exc:
        CommandSpec(id="x", label="x", kind="weird", execute=_dummy_execute)
    assert exc.value.code == "validation_failed"


def test_command_spec_invalid_source_in_allowed_raises():
    with pytest.raises(ZeError) as exc:
        CommandSpec(id="x", label="x", execute=_dummy_execute,
                    sources_allowed=("user", "hacker"))
    assert exc.value.code == "validation_failed"


# ==============================================================
# CommandRegistry register / get / list / has
# ==============================================================

def test_register_basic():
    reg = CommandRegistry()
    spec = CommandSpec(id="x.y", label="x", execute=_dummy_execute)
    reg.register(spec)
    assert reg.has("x.y")
    assert reg.get("x.y") is spec
    assert spec in reg.list()


def test_register_duplicate_raises():
    reg = CommandRegistry()
    reg.register(CommandSpec(id="x", label="x", execute=_dummy_execute))
    with pytest.raises(ZeError) as exc:
        reg.register(CommandSpec(id="x", label="x", execute=_dummy_execute))
    assert exc.value.code == "validation_failed"


def test_register_without_execute_raises():
    reg = CommandRegistry()
    with pytest.raises(ZeError):
        reg.register(CommandSpec(id="x", label="x"))  # execute=None


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("id", ""),
        ("label", ""),
        ("kind", "rollback"),
        ("phase", 0),
        ("touches", [""]),
        ("conflict_strategy", "invented"),
        ("scheduling", "eventually"),
        ("acl", []),
        ("sources_allowed", ("hacker",)),
        ("shortcut", 1),
        ("requires_confirm", 1),
        ("confirm_template", 1),
        ("description", 1),
        ("params_schema", []),
        ("outputs_schema", []),
        ("ui_ports", []),
        ("ai_cost_usd", -1),
        ("ai_high_risk", 1),
        ("poll", "not-callable"),
        ("execute", None),
        ("mutation_authority", 1),
    ),
)
def test_register_revalidates_every_mutable_command_spec_invariant(
    field_name, invalid_value,
):
    registry = CommandRegistry()
    spec = CommandSpec(id="valid.command", label="Valid", execute=_dummy_execute)
    setattr(spec, field_name, invalid_value)

    with pytest.raises(ZeError) as captured:
        registry.register(spec)

    assert captured.value.code == "validation_failed"
    assert registry.list() == []


def test_get_unknown_raises():
    reg = CommandRegistry()
    with pytest.raises(ZeError) as exc:
        reg.get("nope")
    assert exc.value.code == "unknown_command"


def test_has_returns_bool():
    reg = CommandRegistry()
    reg.register(CommandSpec(id="x", label="x", execute=_dummy_execute))
    assert reg.has("x") is True
    assert reg.has("nope") is False


# ==============================================================
# 装饰器
# ==============================================================

def test_command_decorator():
    reg = CommandRegistry()

    @reg.command(id="my.cmd", label="My", kind="native")
    def my_cmd(args, actor, source, doc):
        return _dummy_execute(args, actor, source, doc)

    assert reg.has("my.cmd")
    spec = reg.get("my.cmd")
    assert spec.execute is my_cmd


def test_command_decorator_returns_fn():
    """装饰器返回原函数, 不包装."""
    reg = CommandRegistry()

    @reg.command(id="x", label="x", kind="native")
    def fn(args, actor, source, doc):
        return _dummy_execute(args, actor, source, doc)

    # fn 仍可独立调用
    op = fn({}, _actor(), "user", _new_doc())
    assert isinstance(op, Operation)


# ==============================================================
# can / poll
# ==============================================================

def test_can_returns_true_without_poll():
    reg = CommandRegistry()
    reg.register(CommandSpec(id="x", label="x", execute=_dummy_execute))
    assert reg.can("x", {}) is True


def test_can_calls_poll():
    reg = CommandRegistry()
    reg.register(CommandSpec(
        id="x", label="x", execute=_dummy_execute,
        poll=lambda args: args.get("ready") is True,
    ))
    assert reg.can("x", {"ready": True}) is True
    assert reg.can("x", {"ready": False}) is False
    assert reg.can("x", {}) is False


def test_can_poll_exception_returns_false():
    reg = CommandRegistry()
    reg.register(CommandSpec(
        id="x", label="x", execute=_dummy_execute,
        poll=lambda args: 1 / 0,
    ))
    assert reg.can("x", {}) is False


# ==============================================================
# dispatch 流程
# ==============================================================

def test_dispatch_basic():
    reg = CommandRegistry()
    reg.register(CommandSpec(id="x", label="x", execute=_dummy_execute))
    op = reg.dispatch("x", {}, actor=_actor(), source="user", doc=_new_doc())
    assert isinstance(op, Operation)
    assert op.command_id == "dummy"  # execute return 的 command_id
    assert op.source == "user"


def test_dispatch_unknown_command():
    reg = CommandRegistry()
    with pytest.raises(ZeError) as exc:
        reg.dispatch("nope", {}, actor=_actor(), source="user", doc=_new_doc())
    assert exc.value.code == "unknown_command"


def test_dispatch_source_not_allowed():
    reg = CommandRegistry()
    reg.register(CommandSpec(
        id="x", label="x", execute=_dummy_execute,
        sources_allowed=("user", "system"),
    ))
    with pytest.raises(ZeError) as exc:
        reg.dispatch("x", {}, actor=_actor(), source="ai", doc=_new_doc())
    assert exc.value.code == "acl_denied"


def test_dispatch_poll_false_raises():
    reg = CommandRegistry()
    reg.register(CommandSpec(
        id="x", label="x", execute=_dummy_execute,
        poll=lambda args: False,
    ))
    with pytest.raises(ZeError) as exc:
        reg.dispatch("x", {}, actor=_actor(), source="user", doc=_new_doc())
    assert exc.value.code == "poll_failed"


def test_dispatch_accepts_legacy_op_log_but_does_not_append():
    reg = CommandRegistry()
    reg.register(CommandSpec(id="x", label="x", execute=_dummy_execute))
    log = OpLog()
    op = reg.dispatch("x", {}, actor=_actor(), source="user", doc=_new_doc(), op_log=log)
    assert isinstance(op, Operation)
    assert len(log) == 0


def test_dispatch_without_op_log():
    """不传 op_log 仍返回 op."""
    reg = CommandRegistry()
    reg.register(CommandSpec(id="x", label="x", execute=_dummy_execute))
    op = reg.dispatch("x", {}, actor=_actor(), source="user", doc=_new_doc())
    assert op is not None


def test_dispatch_execute_returns_non_operation_raises():
    reg = CommandRegistry()
    reg.register(CommandSpec(
        id="bad", label="bad",
        execute=lambda args, actor, source, doc: "not an Operation",
    ))
    with pytest.raises(ZeError) as exc:
        reg.dispatch("bad", {}, actor=_actor(), source="user", doc=_new_doc())
    assert exc.value.code == "validation_failed"


def test_dispatch_accepts_explicit_command_mutation():
    operation = _dummy_execute({}, _actor(), "user", _new_doc())
    mutation = CommandMutation((operation,))
    reg = CommandRegistry()
    reg.register(CommandSpec(
        id="batch", label="batch",
        execute=lambda args, actor, source, doc: mutation,
    ))

    result = reg.dispatch("batch", {}, actor=_actor(), source="user", doc=_new_doc())

    assert result is mutation


# ==============================================================
# 内置 commands: register_builtin_commands
# ==============================================================

def test_register_builtin_commands_all():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    builtin_ids = {"entity.add", "entity.batch_add", "entity.update", "entity.batch_update", "entity.delete",
                   "entity.restore", "entity.hard_remove", "entity.cascade_delete", "entity.reorder",  # + 通用拖拽排序
                   "document.rename", "document.set_acl",  # + 文档级 rename/set_acl(权限共享)
                   "notify.push"}                          # + 外发通知(框架自带,推送指定飞书账号)
    assert set(s.id for s in reg.list()) == builtin_ids


def test_document_rename_produces_document_patch():
    """document.rename 改 doc.name 并产出 __document__ 级 set patch(走标准 Operation)。"""
    from operation import DOCUMENT_TARGET
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save", name="旧名")
    op = reg.dispatch(
        "document.rename", {"name": "新名", "expected_version": doc.version},
        actor=_actor(), source="user", doc=doc,
    )
    assert doc.name == "新名"
    assert len(op.patches) == 1
    p = op.patches[0]
    assert p.entity_type == DOCUMENT_TARGET
    assert p.entity_id == doc.id
    assert p.field == "name" and p.op == "set"
    assert p.before == "旧名" and p.after == "新名"
    # reverse 可还原
    assert op.reverse_patches[0].after == "旧名"


def test_document_rename_rejects_empty():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save", name="旧名")
    with pytest.raises(ZeError) as exc:
        reg.dispatch(
            "document.rename", {"name": "   ", "expected_version": doc.version},
            actor=_actor(), source="user", doc=doc,
        )
    assert exc.value.code == "validation_failed"
    assert doc.name == "旧名"  # 未改动


@pytest.mark.parametrize(("command_id", "args"), [
    ("document.rename", {"name": "新名"}),
    ("document.set_acl", {"acl": {"owner": "u1", "collaborators": []}}),
])
def test_document_metadata_commands_require_rendered_version(command_id, args):
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = Document(
        schema_version="1.0.0", collaboration_mode="with_save",
        name="旧名", acl={"owner": "u1", "collaborators": []},
    )
    with pytest.raises(ZeError) as exc:
        reg.dispatch(command_id, args, actor=_actor(), source="user", doc=doc)
    assert exc.value.code == "validation_failed"
    assert doc.name == "旧名"
    assert doc.acl == {"owner": "u1", "collaborators": []}


def _acl_doc(owner="u1", collaborators=("u2",)):
    return Document(
        schema_version="1.0.0", collaboration_mode="with_save", name="d",
        acl={"owner": owner, "collaborators": list(collaborators)},
    )


def test_set_acl_owner_only_is_enforced_in_command_layer():
    """owner-only 校验下沉命令层:owner 直调成功,acl 真被改。"""
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _acl_doc(owner="u1", collaborators=["u2"])
    reg.dispatch(
        "document.set_acl",
        {"acl": {"owner": "u1", "collaborators": ["u2", "u3"]}, "expected_version": doc.version},
        actor=Actor(user_id="u1"), source="user", doc=doc,
    )
    assert doc.acl == {"owner": "u1", "collaborators": ["u2", "u3"]}


def test_set_acl_rejects_non_owner_collaborator():
    """核心漏洞消融:协作者(非 owner)经命令层调 set_acl(镜像 MCP source='ai' 路径)→ acl_denied。

    修前 owner-only 只在 server HTTP 层,协作者+EDIT 经 MCP dispatch 可绕过自设 owner;
    下沉后无论哪个通道到 execute 都被同一门挡下。
    """
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _acl_doc(owner="u1", collaborators=["u2"])
    with pytest.raises(ZeError) as exc:
        reg.dispatch(
            "document.set_acl",
            {"acl": {"owner": "u2", "collaborators": []}, "expected_version": doc.version},
            actor=Actor(user_id="u2"), source="ai", doc=doc,
        )
    assert exc.value.code == "acl_denied"
    assert doc.acl == {"owner": "u1", "collaborators": ["u2"]}   # 未被篡改


def test_set_acl_rejects_normal_user_on_ownerless_project():
    """空 owner(无主公共项目):普通用户不得借 set_acl 把自己设成独占 owner。"""
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _acl_doc(owner="", collaborators=[])
    with pytest.raises(ZeError) as exc:
        reg.dispatch(
            "document.set_acl",
            {"acl": {"owner": "u9", "collaborators": []}, "expected_version": doc.version},
            actor=Actor(user_id="u9"), source="ai", doc=doc,
        )
    assert exc.value.code == "acl_denied"


def test_set_acl_exempts_local_admin_identities():
    """本机全权/运维保留身份(__system__ / __mcp__)豁免 owner-only —— 部署/接管路径不误伤。"""
    from builtin_commands import _ACL_ADMIN_USER_IDS
    for admin_id in _ACL_ADMIN_USER_IDS:
        reg = CommandRegistry()
        register_builtin_commands(reg)
        doc = _acl_doc(owner="u1", collaborators=[])
        reg.dispatch(
            "document.set_acl",
            {"acl": {"owner": "u1", "collaborators": ["u5"]}, "expected_version": doc.version},
            actor=Actor(user_id=admin_id, source="ai"), source="ai", doc=doc,
        )
        assert doc.acl == {"owner": "u1", "collaborators": ["u5"]}


def test_acl_admin_ids_track_framework_reserved_identities():
    """漂移守卫:命令层豁免集必须含 CLI/MCP-loopback 保留 id(否则部署路径静默被误伤)。"""
    import sys as _sys
    _root = str(Path(__file__).resolve().parents[4])
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    try:
        from sdk.bootstrap import SYSTEM_USER_ID
        from sdk.mcp_bridge import MCP_LOCAL_USER_ID
    except Exception:
        pytest.skip("sdk 不在 path(core-only 运行);全量 verify 时校验")
    from builtin_commands import _ACL_ADMIN_USER_IDS
    assert {SYSTEM_USER_ID, MCP_LOCAL_USER_ID} <= _ACL_ADMIN_USER_IDS


def test_register_builtin_idempotent_via_new_registry():
    """同 registry 重复 register 应抛错; 新 registry 可独立注册."""
    reg1 = CommandRegistry()
    register_builtin_commands(reg1)
    with pytest.raises(ZeError):
        register_builtin_commands(reg1)
    # 新 registry 没问题
    reg2 = CommandRegistry()
    register_builtin_commands(reg2)


# ==============================================================
# entity.add
# ==============================================================

def test_entity_add_creates_entity_in_doc():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    op = reg.dispatch(
        "entity.add",
        {"type": "obj", "data": {"name": "x"}, "id": "e1"},
        actor=_actor(), source="user", doc=doc,
    )
    assert doc.find("obj", "e1") is not None
    assert doc.find("obj", "e1").data == {"name": "x"}


def test_entity_add_returns_operation_with_create_patch():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    op = reg.dispatch(
        "entity.add",
        {"type": "obj", "data": {"name": "x"}, "id": "e1"},
        actor=_actor(), source="user", doc=doc,
    )
    assert op.command_id == "entity.add"
    assert op.document_id == doc.id
    assert len(op.patches) == 1
    p = op.patches[0]
    assert p.field == ENTITY_META_FIELD
    assert p.op == "create"
    assert p.before is None
    assert p.after["id"] == "e1"


def test_entity_add_reverse_patch_is_delete():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    op = reg.dispatch(
        "entity.add",
        {"type": "obj", "data": {}, "id": "e1"},
        actor=_actor(), source="user", doc=doc,
    )
    rev = op.reverse_patches[0]
    assert rev.op == "delete"
    assert rev.after is None


def test_entity_batch_add_creates_n_in_one_operation():
    """反哺 D:entity.batch_add 一次建 N 个(可混型),单 Operation;reverse=倒序 delete。"""
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    op = reg.dispatch(
        "entity.batch_add",
        {"items": [
            {"type": "obj", "name": "a", "data": {"k": 1}, "id": "e1"},
            {"type": "obj", "name": "b", "data": {"k": 2}, "id": "e2"},
        ]},
        actor=_actor(), source="ai", doc=doc,
    )
    assert doc.find("obj", "e1") is not None and doc.find("obj", "e2") is not None
    assert op.command_id == "entity.batch_add"
    assert len(op.patches) == 2 and all(p.op == "create" for p in op.patches)
    assert [p.entity_id for p in op.patches] == ["e1", "e2"]
    assert [p.op for p in op.reverse_patches] == ["delete", "delete"]
    assert [p.entity_id for p in op.reverse_patches] == ["e2", "e1"]   # 倒序删


def test_entity_batch_add_rejects_empty_and_missing_type():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    for bad in ([], [{"name": "no-type"}]):
        try:
            reg.dispatch("entity.batch_add", {"items": bad}, actor=_actor(), source="ai", doc=doc)
            assert False, "should have raised"
        except ZeError as e:
            assert e.code == "validation_failed"


def test_entity_batch_add_on_conflict_skip_is_idempotent():
    """on_conflict='skip':已存在同 type+id 的项静默跳过(不产 patch、不覆盖);
    默认('error')行为不变=撞车抛错。根治 stage 落库竞态(D3 自动建帧 TOCTOU 反哺)。"""
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch("entity.batch_add",
                 {"items": [{"type": "obj", "id": "e1", "data": {"k": 1}}]},
                 actor=_actor(), source="ai", doc=doc)
    # 默认 error:重复 id 仍抛(向后兼容)
    try:
        reg.dispatch("entity.batch_add",
                     {"items": [{"type": "obj", "id": "e1", "data": {"k": 9}}]},
                     actor=_actor(), source="ai", doc=doc)
        assert False, "duplicate id should raise under default on_conflict"
    except ZeError:
        pass
    # skip:同 id 跳过、新 id 落库;patch 只含真正建的
    op = reg.dispatch("entity.batch_add",
                      {"items": [{"type": "obj", "id": "e1", "data": {"k": 9}},
                                 {"type": "obj", "id": "e2", "data": {"k": 2}}],
                       "on_conflict": "skip"},
                      actor=_actor(), source="ai", doc=doc)
    assert doc.get("obj", "e1").data["k"] == 1      # 未被覆盖
    assert doc.find("obj", "e2") is not None
    assert [p.entity_id for p in op.patches] == ["e2"]
    # 全部已存在 → 空 patches 的合法 Operation(幂等重放)
    op2 = reg.dispatch("entity.batch_add",
                       {"items": [{"type": "obj", "id": "e1"}, {"type": "obj", "id": "e2"}],
                        "on_conflict": "skip"},
                       actor=_actor(), source="ai", doc=doc)
    assert len(op2.patches) == 0 and len(op2.reverse_patches) == 0
    # 非法值拒
    try:
        reg.dispatch("entity.batch_add",
                     {"items": [{"type": "obj", "id": "e3"}], "on_conflict": "merge"},
                     actor=_actor(), source="ai", doc=doc)
        assert False, "invalid on_conflict should raise"
    except ZeError as e:
        assert e.code == "validation_failed"


# ==============================================================
# entity.update
# ==============================================================

def test_entity_update_modifies_data():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch("entity.add",
                 {"type": "obj", "data": {"a": 1}, "id": "e1"},
                 actor=_actor(), source="user", doc=doc)
    e = doc.get("obj", "e1")
    op = reg.dispatch(
        "entity.update",
        {"type": "obj", "id": "e1", "patches": {"a": 2}, "expected_version": e.version},
        actor=_actor(), source="user", doc=doc,
    )
    assert doc.get("obj", "e1").data == {"a": 2}
    assert op.command_id == "entity.update"


def test_entity_update_reverse_has_before_after_swapped():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch("entity.add",
                 {"type": "obj", "data": {"a": 1}, "id": "e1"},
                 actor=_actor(), source="user", doc=doc)
    e = doc.get("obj", "e1")
    op = reg.dispatch(
        "entity.update",
        {"type": "obj", "id": "e1", "patches": {"a": 99}, "expected_version": e.version},
        actor=_actor(), source="user", doc=doc,
    )
    fwd = op.patches[0]
    rev = op.reverse_patches[0]
    assert fwd.before == 1 and fwd.after == 99
    assert rev.before == 99 and rev.after == 1


def test_entity_update_rename_is_undoable_round_trip():
    """改名进 Operation:fwd/rev 各一个 op='name' 元 patch → undo 还原旧名、redo 重做新名(簇 A 回归)。"""
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch("entity.add",
                 {"type": "obj", "data": {"a": 1}, "id": "e1", "name": "orig"},
                 actor=_actor(), source="user", doc=doc)
    e = doc.get("obj", "e1")
    assert e.name == "orig"
    op = reg.dispatch(
        "entity.update",
        {"type": "obj", "id": "e1", "patches": {"a": 2}, "name": "renamed",
         "expected_version": e.version},
        actor=_actor(), source="user", doc=doc,
    )
    assert doc.get("obj", "e1").name == "renamed"
    # fwd/rev 各含一个 name 元 patch(此前 156-160 是死代码 → 改名根本不进 patch)
    fwd_name = [p for p in op.patches if p.op == "name"]
    rev_name = [p for p in op.reverse_patches if p.op == "name"]
    assert len(fwd_name) == 1 and fwd_name[0].field == ENTITY_META_FIELD
    assert fwd_name[0].before == "orig" and fwd_name[0].after == "renamed"
    assert len(rev_name) == 1 and rev_name[0].after == "orig"
    # undo:重放 reverse_patches → 名字回 orig(此前永远回不去)
    for p in op.reverse_patches:
        _apply_patch(doc, p, actor=_actor())
    assert doc.get("obj", "e1").name == "orig"
    # redo:重放 forward patches → 名字回 renamed
    for p in op.patches:
        _apply_patch(doc, p, actor=_actor())
    assert doc.get("obj", "e1").name == "renamed"


def test_entity_update_without_name_change_emits_no_name_patch():
    """不传 name → 不产生 name patch(避免噪声 patch / 防误把 None 当改名)。"""
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch("entity.add",
                 {"type": "obj", "data": {"a": 1}, "id": "e1", "name": "orig"},
                 actor=_actor(), source="user", doc=doc)
    e = doc.get("obj", "e1")
    op = reg.dispatch(
        "entity.update",
        {"type": "obj", "id": "e1", "patches": {"a": 2}, "expected_version": e.version},
        actor=_actor(), source="user", doc=doc,
    )
    assert not any(p.op == "name" for p in op.patches)
    assert not any(p.op == "name" for p in op.reverse_patches)


# ==============================================================
# entity.batch_update — 单 Operation 批量(整体拒绝 + 回滚)
# ==============================================================

def _batch_doc(reg):
    """三个同型 entity 的 doc(a 值各异)。"""
    doc = _new_doc()
    for i, eid in enumerate(("e1", "e2", "e3"), start=1):
        reg.dispatch("entity.add",
                     {"type": "obj", "data": {"a": i, "b": "x"}, "id": eid},
                     actor=_actor(), source="user", doc=doc)
    return doc


def test_batch_update_single_operation_multi_entity():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _batch_doc(reg)
    items = [{"id": eid, "patches": {"a": 9}, "expected_version": doc.get("obj", eid).version}
             for eid in ("e1", "e2", "e3")]
    op = reg.dispatch("entity.batch_update", {"type": "obj", "items": items},
                      actor=_actor(), source="user", doc=doc)
    assert op.command_id == "entity.batch_update"
    assert all(doc.get("obj", eid).data["a"] == 9 for eid in ("e1", "e2", "e3"))
    # 单 Operation 覆盖 3 entity, 每 entity 一个 set patch, reverse before/after 互换
    assert len(op.patches) == 3 and len(op.reverse_patches) == 3
    assert {p.entity_id for p in op.patches} == {"e1", "e2", "e3"}
    rev_by_id = {p.entity_id: p for p in op.reverse_patches}
    assert rev_by_id["e2"].before == 9 and rev_by_id["e2"].after == 2


def test_batch_update_cas_mismatch_rejects_whole_batch():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _batch_doc(reg)
    good_v = doc.get("obj", "e1").version
    items = [
        {"id": "e1", "patches": {"a": 9}, "expected_version": good_v},
        {"id": "e2", "patches": {"a": 9}, "expected_version": 999},   # 版本错
    ]
    with pytest.raises(ZeError) as exc:
        reg.dispatch("entity.batch_update", {"type": "obj", "items": items},
                     actor=_actor(), source="user", doc=doc)
    assert exc.value.code == "cas_version_mismatch"
    # 整体拒绝: e1 也未被改
    assert doc.get("obj", "e1").data["a"] == 1
    assert doc.get("obj", "e1").version == good_v


def test_batch_update_read_only_rejected_in_precheck_zero_mutation():
    """预检覆盖可写性: read_only 项整体拒绝, 任何项零变更(连版本都不动)。"""
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _batch_doc(reg)
    reg.dispatch("entity.add",
                 {"type": "obj", "data": {"a": 0}, "id": "ro", "read_only": True},
                 actor=_actor(), source="user", doc=doc)
    v1 = doc.get("obj", "e1").version
    items = [
        {"id": "e1", "patches": {"a": 9}, "expected_version": v1},
        {"id": "ro", "patches": {"a": 9}, "expected_version": doc.get("obj", "ro").version},
    ]
    with pytest.raises(ZeError) as exc:
        reg.dispatch("entity.batch_update", {"type": "obj", "items": items},
                     actor=_actor(), source="user", doc=doc)
    assert exc.value.code == "read_only_violation"
    assert doc.get("obj", "e1").data["a"] == 1
    assert doc.get("obj", "e1").version == v1      # 预检拒绝 = 零变更
    assert doc.get("obj", "ro").data["a"] == 0


def test_batch_update_midway_failure_restores_data_and_version(monkeypatch):
    """apply 途中失败(注入;且为非 ZeError,验证 except 全覆盖)→ 已应用项 data **和
    version** 精确还原(否则并发客户端持原版本会被假 CAS 冲突击中)。"""
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _batch_doc(reg)
    v1 = doc.get("obj", "e1").version
    v2 = doc.get("obj", "e2").version

    def boom(*a, **k):
        raise RuntimeError("injected mid-apply failure")   # 非 ZeError 也必须回滚

    real_update = Entity.update

    def fail_second_entity(self, *args, **kwargs):
        if self.id == "e2":
            return boom(*args, **kwargs)
        return real_update(self, *args, **kwargs)

    monkeypatch.setattr(Entity, "update", fail_second_entity)

    items = [
        {"id": "e1", "patches": {"a": 9}, "expected_version": v1},   # 合法, 先应用
        {"id": "e2", "patches": {"a": 9}, "expected_version": v2},   # 注入失败
    ]
    with pytest.raises(RuntimeError):
        reg.dispatch("entity.batch_update", {"type": "obj", "items": items},
                     actor=_actor(), source="user", doc=doc)
    assert doc.get("obj", "e1").data["a"] == 1
    assert doc.get("obj", "e1").version == v1      # 版本精确还原(非 +2)
    assert doc.get("obj", "e2").data["a"] == 2
    assert doc.get("obj", "e2").version == v2


def test_batch_update_empty_items_rejected():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    with pytest.raises(ZeError) as exc:
        reg.dispatch("entity.batch_update", {"type": "obj", "items": []},
                     actor=_actor(), source="user", doc=doc)
    assert exc.value.code == "validation_failed"


def test_batch_update_dot_path_before_is_subvalue_undo_restores_nested():
    """dot-path key 'box.w':fwd/rev 的 before 取嵌套子值(非 None),undo 精确还原子键不写坏(簇 B)。"""
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch("entity.add",
                 {"type": "obj", "data": {"box": {"w": 10, "h": 20}}, "id": "e1"},
                 actor=_actor(), source="user", doc=doc)
    e = doc.get("obj", "e1")
    op = reg.dispatch(
        "entity.batch_update",
        {"type": "obj", "items": [{"id": "e1", "patches": {"box.w": 99}, "expected_version": e.version}]},
        actor=_actor(), source="user", doc=doc,
    )
    assert doc.get("obj", "e1").data["box"] == {"w": 99, "h": 20}
    fwd = next(p for p in op.patches if p.field == "box.w")
    rev = next(p for p in op.reverse_patches if p.field == "box.w")
    assert fwd.before == 10 and fwd.after == 99          # 此前 before=None(扁平 .get 取不到嵌套)
    assert rev.before == 99 and rev.after == 10
    # undo:重放 reverse → box.w 回 10、box.h 不动(此前会被写成 None)
    for p in op.reverse_patches:
        _apply_patch(doc, p, actor=_actor())
    assert doc.get("obj", "e1").data["box"] == {"w": 10, "h": 20}


# ==============================================================
# entity.delete / restore
# ==============================================================

def test_entity_delete_soft_deletes():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch("entity.add", {"type": "obj", "data": {}, "id": "e1"},
                 actor=_actor(), source="user", doc=doc)
    e = doc.get("obj", "e1")
    op = reg.dispatch(
        "entity.delete",
        {"type": "obj", "id": "e1", "expected_version": e.version},
        actor=_actor(), source="user", doc=doc,
    )
    assert doc.get("obj", "e1").deleted is True
    assert op.patches[0].op == "soft_delete"
    assert op.reverse_patches[0].op == "restore"


def test_entity_restore_undeletes():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch("entity.add", {"type": "obj", "data": {}, "id": "e1"},
                 actor=_actor(), source="user", doc=doc)
    e = doc.get("obj", "e1")
    reg.dispatch("entity.delete",
                 {"type": "obj", "id": "e1", "expected_version": e.version},
                 actor=_actor(), source="user", doc=doc)
    e = doc.get("obj", "e1")
    op = reg.dispatch(
        "entity.restore",
        {"type": "obj", "id": "e1", "expected_version": e.version},
        actor=_actor(), source="user", doc=doc,
    )
    assert doc.get("obj", "e1").deleted is False
    assert op.patches[0].op == "restore"
    assert op.reverse_patches[0].op == "soft_delete"


# ==============================================================
# entity.hard_remove
# ==============================================================

def test_entity_hard_remove_removes_from_doc():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch("entity.add", {"type": "obj", "data": {}, "id": "e1"},
                 actor=_actor(), source="user", doc=doc)
    op = reg.dispatch(
        "entity.hard_remove",
        {"type": "obj", "id": "e1"},
        actor=_actor(), source="user", doc=doc,
    )
    assert doc.find("obj", "e1") is None
    assert op.patches[0].op == "delete"
    assert op.reverse_patches[0].op == "create"
    assert op.reverse_patches[0].after["id"] == "e1"


def test_entity_hard_remove_denied_for_ai_source():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch("entity.add", {"type": "obj", "data": {}, "id": "e1"},
                 actor=_actor(), source="user", doc=doc)
    with pytest.raises(ZeError) as exc:
        reg.dispatch(
            "entity.hard_remove",
            {"type": "obj", "id": "e1"},
            actor=_actor(), source="ai", doc=doc,
        )
    assert exc.value.code == "acl_denied"


# ==============================================================
# entity.cascade_delete
# ==============================================================

def test_entity_cascade_delete_soft_deletes_chain():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch("entity.add",
                 {"type": "a", "data": {"r": {"$ref": "b/2"}}, "id": "1"},
                 actor=_actor(), source="user", doc=doc)
    reg.dispatch("entity.add",
                 {"type": "b", "data": {}, "id": "2"},
                 actor=_actor(), source="user", doc=doc)
    op = reg.dispatch(
        "entity.cascade_delete",
        {"type": "a", "id": "1"},
        actor=_actor(), source="user", doc=doc,
    )
    assert doc.find("a", "1").deleted is True
    assert doc.find("b", "2").deleted is True
    # 至少有 2 个 soft_delete patches
    assert len(op.patches) >= 2
    assert all(p.op == "soft_delete" for p in op.patches)


# ==============================================================
# 集成: dispatch 连续执行不偷做提交
# ==============================================================

def test_full_flow_dispatch_mutates_document_but_leaves_oplog_to_coordinator():
    """add → update → delete 只执行 handler；OpLog 由上层事务提交。"""
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    log = OpLog()

    add_op = reg.dispatch("entity.add",
                          {"type": "obj", "data": {"a": 1}, "id": "e1"},
                          actor=_actor(), source="user", doc=doc, op_log=log)
    e = doc.get("obj", "e1")
    update_op = reg.dispatch("entity.update",
                             {"type": "obj", "id": "e1", "patches": {"a": 2},
                              "expected_version": e.version},
                             actor=_actor(), source="ai", doc=doc, op_log=log)
    e = doc.get("obj", "e1")
    delete_op = reg.dispatch("entity.delete",
                             {"type": "obj", "id": "e1", "expected_version": e.version},
                             actor=_actor(), source="user", doc=doc, op_log=log)

    assert [add_op.command_id, update_op.command_id, delete_op.command_id] == [
        "entity.add", "entity.update", "entity.delete",
    ]
    assert doc.get("obj", "e1").deleted is True
    assert len(log) == 0


# ==============================================================
# entity.update — dot-path 嵌套补丁 (P2 决定1)
# ==============================================================

def test_entity_update_dotpath_roundtrip():
    """dot-path 子键改:前向落子值(兄弟键保留)、reverse 精确还原(round-trip)。"""
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch(
        "entity.add",
        {"type": "obj", "id": "e1", "data": {"transform2d": {"x": 1, "y": 2}}},
        actor=_actor(), source="user", doc=doc,
    )
    op = reg.dispatch(
        "entity.update",
        {"type": "obj", "id": "e1", "patches": {"transform2d.x": 100}, "expected_version": 1},
        actor=_actor(), source="user", doc=doc,
    )
    # 前向:子键改、兄弟键保留
    assert doc.find("obj", "e1").data["transform2d"] == {"x": 100, "y": 2}
    # patch 形态:field=dot-path 串,before/after=子值
    p = op.patches[0]
    assert p.field == "transform2d.x" and p.before == 1 and p.after == 100
    rp = op.reverse_patches[0]
    assert rp.field == "transform2d.x" and rp.before == 100 and rp.after == 1
    # 应用 reverse(undo/sync 同路)→ 完全复原
    _apply_patch(doc, rp, actor=_actor())
    assert doc.find("obj", "e1").data["transform2d"] == {"x": 1, "y": 2}


def test_entity_update_dotpath_multi_one_operation():
    """同 update 多条 dot-path → 各一条 patch,折叠到同一字段,一个 Operation 可一键撤销。"""
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    reg.dispatch(
        "entity.add",
        {"type": "obj", "id": "e1", "data": {"t": {"x": 0, "y": 0, "z": 9}}},
        actor=_actor(), source="user", doc=doc,
    )
    op = reg.dispatch(
        "entity.update",
        {"type": "obj", "id": "e1", "patches": {"t.x": 10, "t.y": 20}, "expected_version": 1},
        actor=_actor(), source="user", doc=doc,
    )
    assert doc.find("obj", "e1").data["t"] == {"x": 10, "y": 20, "z": 9}
    assert {p.field for p in op.patches} == {"t.x", "t.y"}
    # 一键撤销:倒序应用 reverse → 复原
    for rp in reversed(op.reverse_patches):
        _apply_patch(doc, rp, actor=_actor())
    assert doc.find("obj", "e1").data["t"] == {"x": 0, "y": 0, "z": 9}


# ==============================================================
# entity.reorder (通用拖拽排序)
# ==============================================================

def _order_ids(doc):
    """按 order 字段排序后的 id 列(缺失排末尾、按插入序兜底)—— 镜像 GridZone 显示序。"""
    items = list(enumerate(doc.list("obj")))
    items.sort(key=lambda p: (p[1].data.get("order", float("inf")), p[0]))
    return [e.id for _, e in items]


def test_reorder_moves_before_target_and_normalizes():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _batch_doc(reg)                       # e1,e2,e3(无 order)
    op = reg.dispatch("entity.reorder", {"type": "obj", "id": "e3", "before_id": "e1"},
                      actor=_actor(), source="user", doc=doc)
    assert op.command_id == "entity.reorder"
    assert _order_ids(doc) == ["e3", "e1", "e2"]          # e3 移到 e1 前
    assert doc.find("obj", "e3").data["order"] == 0
    assert doc.find("obj", "e1").data["order"] == 1
    assert doc.find("obj", "e2").data["order"] == 2


def test_reorder_before_none_moves_to_end():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _batch_doc(reg)
    reg.dispatch("entity.reorder", {"type": "obj", "id": "e1", "before_id": None},
                 actor=_actor(), source="user", doc=doc)
    assert _order_ids(doc) == ["e2", "e3", "e1"]          # e1 到末尾


def test_reorder_is_single_operation_undoable():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _batch_doc(reg)
    op = reg.dispatch("entity.reorder", {"type": "obj", "id": "e3", "before_id": "e1"},
                      actor=_actor(), source="user", doc=doc)
    for rp in reversed(op.reverse_patches):               # 一键撤销
        _apply_patch(doc, rp, actor=_actor())
    # order 回到应用前(此前无 order 字段 → 撤销后仍无 → 显示回插入序)
    assert _order_ids(doc) == ["e1", "e2", "e3"]


def test_reorder_noop_when_already_in_place():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _batch_doc(reg)
    reg.dispatch("entity.reorder", {"type": "obj", "id": "e3", "before_id": "e1"},
                 actor=_actor(), source="user", doc=doc)
    v_before = {e: doc.find("obj", e).version for e in ("e1", "e2", "e3")}
    op = reg.dispatch("entity.reorder", {"type": "obj", "id": "e3", "before_id": "e1"},
                      actor=_actor(), source="user", doc=doc)   # 已在位
    assert not op.patches                                  # 零变更(patches 为空)
    assert {e: doc.find("obj", e).version for e in ("e1", "e2", "e3")} == v_before  # 版本不动


def test_reorder_preserves_relative_order_of_untouched():
    reg = CommandRegistry()
    register_builtin_commands(reg)
    doc = _new_doc()
    for eid in ("a", "b", "c", "d"):
        reg.dispatch("entity.add", {"type": "obj", "data": {}, "id": eid},
                     actor=_actor(), source="user", doc=doc)
    reg.dispatch("entity.reorder", {"type": "obj", "id": "d", "before_id": "b"},
                 actor=_actor(), source="user", doc=doc)
    assert _order_ids(doc) == ["a", "d", "b", "c"]         # a/b/c 相对序不乱
