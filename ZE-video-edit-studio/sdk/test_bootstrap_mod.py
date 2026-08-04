"""sdk/test_bootstrap_mod.py — _load_mod 多文件 mod 载入单测(D1).

回归:`_load_mod` 用 spec_from_file_location 载入的模块没有包上下文,mod 文件内
`import 同级模块` 会 ModuleNotFoundError。修法是载入前把 MOD_CODE 注入 sys.path。
本测构造临时双文件 mod(a `import b`)经 `_load_mod` 验证可载入,且单文件 mod 不回归。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sdk import bootstrap  # noqa: E402


def _write_mod(tmp_path, files: dict) -> Path:
    """在临时目录铺一套 mod 文件,返回该目录(供 monkeypatch MOD_CODE)。"""
    mod_dir = tmp_path / "05-code"
    mod_dir.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (mod_dir / name).write_text(body, encoding="utf-8")
    return mod_dir


def test_multifile_mod_sibling_import(tmp_path, monkeypatch):
    """双文件 mod:a_register.py `import helper_b` → 经 _load_mod 成功载入并取到符号。"""
    mod_dir = _write_mod(tmp_path, {
        "helper_b.py": "VALUE = 42\n",
        "a_register.py": "import helper_b\nRESULT = helper_b.VALUE + 1\n",
    })
    monkeypatch.setattr(bootstrap, "MOD_CODE", mod_dir)
    # 清掉可能的同名缓存,确保走真载入
    sys.modules.pop("helper_b", None)

    module = bootstrap._load_mod("a_register.py")
    assert module.RESULT == 43, "同级 import 应成功并求值"
    assert str(mod_dir) in sys.path, "MOD_CODE 应被注入 sys.path"


def test_singlefile_mod_no_regression(tmp_path, monkeypatch):
    """单文件 mod(无同级 import)仍正常载入 —— 加性改动不破坏既有路径。"""
    mod_dir = _write_mod(tmp_path, {
        "solo_register.py": "MARKER = 'ok'\n",
    })
    monkeypatch.setattr(bootstrap, "MOD_CODE", mod_dir)

    module = bootstrap._load_mod("solo_register.py")
    assert module.MARKER == "ok"


def test_load_mod_idempotent_syspath(tmp_path, monkeypatch):
    """重复 _load_mod 不重复往 sys.path 插同一 MOD_CODE(幂等)。"""
    mod_dir = _write_mod(tmp_path, {"x_register.py": "N = 1\n"})
    monkeypatch.setattr(bootstrap, "MOD_CODE", mod_dir)

    bootstrap._load_mod("x_register.py")
    bootstrap._load_mod("x_register.py")
    assert sys.path.count(str(mod_dir)) == 1, "同一 MOD_CODE 只应注入一次"


# ---- on-open 文档迁移钩子(declare_open_migration + build_project_context 装载后调用)----

def _stamp_mod_files() -> dict:
    """临时 mod:声明 widget 实体 + 一个 on-open 迁移 —— 给未打戳的 widget 补 stamped=True。

    这是「框架机制/零业务」钩子的最小消费者:验证 build_project_context 在装载持久化项目后
    真的调用了 mod 声明的迁移、且返 True 时把改动落盘(下游 batch-image-foundry 的 schema 迁移
    走的正是这条通道)。
    """
    document_register = (
        "from sdk import DocumentRegistry, Entity\n"
        "\n"
        "def _stamp_migration(doc):\n"
        "    changed = False\n"
        "    for e in doc.list('widget', include_deleted=True):\n"
        "        d = getattr(e, '_data', None)\n"
        "        if isinstance(d, dict) and not d.get('stamped'):\n"
        "            d['stamped'] = True\n"
        "            changed = True\n"
        "    return changed\n"
        "\n"
        "def register(reg):\n"
        "    reg.declare_entity_type(name='widget', schema={'type': 'object', "
        "'properties': {'stamped': {'type': 'boolean', 'label': 'S'}}})\n"
        "    reg.set_collaboration_mode('with_save')\n"
        "    reg.set_save_policy('auto_commit')\n"
        "    reg.declare_open_migration(_stamp_migration)\n"
    )
    commands_register = "def register(reg):\n    pass\n"
    return {"document_register.py": document_register,
            "commands_register.py": commands_register}


def _policy_mod_files(*, seed_count: int = 0, command_id: str = "Widget.Update") -> dict:
    seed_lines = "".join(
        f"    doc.add(Entity(type='widget', id='w{index}', data={{'value': {index}}}))\n"
        for index in range(seed_count)
    ) or "    pass\n"
    document_register = (
        "from sdk import Entity\n"
        "\n"
        "def _seed(doc):\n"
        f"{seed_lines}"
        "\n"
        "def register(reg):\n"
        "    reg.declare_entity_type(\n"
        "        name='widget',\n"
        "        schema={'type': 'object', 'properties': {'value': {'type': 'integer'}}},\n"
        "        mutation_policy={\n"
        "            'schema': 'ze.entity-mutation-policy.v1',\n"
        "            'lifecycle': {'create': [], 'rename': [], 'soft_delete': [], "
        "'restore': [], 'hard_remove': []},\n"
        f"            'fields': {{'value': ['{command_id}']}},\n"
        "            'cardinality': {'max_total': 1},\n"
        "        },\n"
        "    )\n"
        "    reg.set_collaboration_mode('with_save')\n"
        "    reg.set_save_policy('auto_commit')\n"
        "    reg.set_demo_project(name='policy demo', bootstrap=_seed)\n"
    )
    commands_register = "def register(reg):\n    pass\n"
    return {
        "document_register.py": document_register,
        "commands_register.py": commands_register,
    }


def _policy_authority_mod_files() -> dict:
    files = _policy_mod_files(seed_count=1)
    files["commands_register.py"] = (
        "from sdk import CommandSpec, Patch, new_operation\n"
        "\n"
        "def _update(args, actor, source, doc):\n"
        "    entity = doc.get('widget', args['id'])\n"
        "    before = entity.data['value']\n"
        "    after = args['value']\n"
        "    entity.update({'value': after}, expected_version=entity.version, actor=actor)\n"
        "    return new_operation(\n"
        "        command_id='Widget.Update', source=source, actor=actor.to_dict(),\n"
        "        patches=[Patch('widget', entity.id, 'value', 'set', before, after)],\n"
        "        reverse_patches=[Patch('widget', entity.id, 'value', 'set', after, before)],\n"
        "    )\n"
        "\n"
        "def register(reg):\n"
        "    reg.register(CommandSpec(\n"
        "        id='Widget.Update', label='Update widget',\n"
        "        mutation_authority=True, execute=_update,\n"
        "    ))\n"
    )
    return files


def _policy_overflow_migration_mod_files() -> dict:
    files = _policy_mod_files()
    source = files["document_register.py"]
    source = source.replace(
        "def register(reg):\n",
        "def _overflow(doc):\n"
        "    doc.add(Entity(type='widget', id='migration-extra', data={'value': 2}))\n"
        "    return True\n"
        "\n"
        "def register(reg):\n",
    )
    source = source.replace(
        "    reg.set_demo_project(name='policy demo', bootstrap=_seed)\n",
        "    reg.set_demo_project(name='policy demo', bootstrap=_seed)\n"
        "    reg.declare_open_migration(_overflow)\n",
    )
    files["document_register.py"] = source
    return files


def _seed_project(bootstrap_mod, repo, pid: str, *, widget_data: dict):
    """落一个启动前遗留项目并显式跑 topology adoption。

    W05 后运行中凭空出现的无 marker 目录不再被读取；那会绕过 namespace
    coordinator。这里仍要测试旧文档 on-open 迁移，因此在直写 fixture 后重跑
    与 Repository 启动相同的 adoption 边界。
    """
    from sdk import Document, DocumentRegistry, Entity
    reg = DocumentRegistry()
    bootstrap_mod._load_mod("document_register.py").register(reg)
    doc = reg.build_document(schema_version="1.0", id=pid, name="t",
                            acl={"owner": "u", "collaborators": []})
    doc.add(Entity(type="widget", id="w1", name="W", data=dict(widget_data)))
    proj_dir = repo.projects_root / pid
    proj_dir.mkdir(parents=True, exist_ok=True)
    doc.save(proj_dir / "document.json")
    repo.topology.initialize(validate=lambda root: Document.open(root / "document.json"))
    return proj_dir


def test_open_migration_runs_on_load_and_persists(tmp_path, monkeypatch):
    """装载持久化项目 → build_project_context 依次调用声明的 on-open 迁移,返 True 即落盘。"""
    import json
    from sdk import Document

    mod_dir = _write_mod(tmp_path, _stamp_mod_files())
    monkeypatch.setattr(bootstrap, "MOD_CODE", mod_dir)
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))

    repo = bootstrap.bootstrap()
    proj_dir = _seed_project(bootstrap, repo, "proj1", widget_data={})  # 旧数据:缺 stamped

    ctx = repo.get_or_load("proj1")                                     # 首次装载 → 迁移跑
    assert ctx.doc.get("widget", "w1").data.get("stamped") is True      # 内存已迁移
    reopened = Document.open(proj_dir / "document.json")
    assert reopened.get("widget", "w1").data["stamped"] is True       # 已落盘持久化


def test_recovery_failure_retires_unpublished_history_before_retry(
    tmp_path, monkeypatch,
):
    """post-bind recovery 失败不能把 canonical OpLog path 永久占住。"""
    mod_dir = _write_mod(tmp_path, _stamp_mod_files())
    monkeypatch.setattr(bootstrap, "MOD_CODE", mod_dir)
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))
    repo = bootstrap.bootstrap()
    _seed_project(bootstrap, repo, "recover-retry", widget_data={"stamped": True})

    real_recover = bootstrap.PipelineSupervisor.recover
    calls = 0

    def fail_once(supervisor):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected recovery failure")
        return real_recover(supervisor)

    monkeypatch.setattr(bootstrap.PipelineSupervisor, "recover", fail_once)
    with pytest.raises(RuntimeError, match="injected recovery failure"):
        repo.get_or_load("recover-retry")

    context = repo.get_or_load("recover-retry")
    assert context.doc.id == "recover-retry"
    assert context.history_authority.state == "ACTIVE"
    assert context.doc._history_op_log_authority is context.op_log
    assert context.doc._history_controller_authority is context.undo
    assert (
        context.doc._history_authority_epoch
        is context.undo._history_authority_epoch
    )
    assert context.doc._history_authority_pid == context.undo._history_authority_pid
    assert context.commit.document is context.doc
    assert context.commit.store.op_log is context.op_log
    assert calls == 2


def test_open_migration_idempotent_no_rewrite(tmp_path, monkeypatch):
    """已是当前态的项目再装载:迁移返 False → 框架跳过 save(doc version 不变,幂等不抖)。"""
    import json

    mod_dir = _write_mod(tmp_path, _stamp_mod_files())
    monkeypatch.setattr(bootstrap, "MOD_CODE", mod_dir)
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))

    repo = bootstrap.bootstrap()
    proj_dir = _seed_project(bootstrap, repo, "proj2", widget_data={"stamped": True})
    v0 = json.loads((proj_dir / "document.json").read_text())["_v"]   # 文档版本(save 成功 +1)

    ctx = repo.get_or_load("proj2")
    assert ctx.doc.get("widget", "w1").data.get("stamped") is True     # 不变、不报错
    v1 = json.loads((proj_dir / "document.json").read_text())["_v"]
    assert v1 == v0, "no-op 迁移不应触发落盘(save 会 bump _v)"


def test_open_adopts_mod_save_policy_for_legacy_document(tmp_path, monkeypatch):
    """旧项目缺 save_policy 时，装载会采用当前 mod 声明并持久化一次。"""
    import json

    mod_dir = _write_mod(tmp_path, _stamp_mod_files())
    monkeypatch.setattr(bootstrap, "MOD_CODE", mod_dir)
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))

    repo = bootstrap.bootstrap()
    proj_dir = _seed_project(bootstrap, repo, "proj-policy", widget_data={"stamped": True})
    path = proj_dir / "document.json"
    raw = json.loads(path.read_text())
    before_version = raw["_v"]
    raw.pop("save_policy", None)  # 模拟 W02 之前的持久文档
    path.write_text(json.dumps(raw), encoding="utf-8")

    ctx = repo.get_or_load("proj-policy")

    assert ctx.doc.save_policy == "auto_commit"
    persisted = json.loads(path.read_text())
    assert persisted["save_policy"] == "auto_commit"
    assert persisted["_v"] == before_version + 1


def test_project_seed_must_satisfy_declared_cardinality_before_publish(
    tmp_path, monkeypatch,
):
    mod_dir = _write_mod(tmp_path, _policy_mod_files(seed_count=2))
    monkeypatch.setattr(bootstrap, "MOD_CODE", mod_dir)
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))
    repo = bootstrap.bootstrap()

    import pytest

    with pytest.raises(bootstrap.ZeError) as captured:
        repo.create_project(
            name="too many", owner="u", seed=True, project_id="policy-overflow",
        )

    assert captured.value.code == "mutation_cardinality_violation"
    assert not (repo.projects_root / "policy-overflow").exists()


def test_policy_command_references_fail_before_pipeline_recovery(
    tmp_path, monkeypatch,
):
    mod_dir = _write_mod(
        tmp_path, _policy_mod_files(command_id="Missing.Widget.Update"),
    )
    monkeypatch.setattr(bootstrap, "MOD_CODE", mod_dir)
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))
    recovered = []
    monkeypatch.setattr(
        bootstrap.PipelineSupervisor,
        "recover",
        lambda self: recovered.append(self),
    )
    repo = bootstrap.bootstrap()
    repo.create_project(
        name="policy", owner="u", seed=False, project_id="policy-missing-command",
    )

    import pytest

    with pytest.raises(bootstrap.ZeError) as captured:
        repo.get_or_load("policy-missing-command")

    assert captured.value.code == "mutation_policy_invalid"
    assert captured.value.context["reason"] == "mutation_policy_command_unregistered"
    assert recovered == []


def test_explicit_mutation_authority_bootstrap_seals_and_executes_via_coordinator(
    tmp_path, monkeypatch,
):
    mod_dir = _write_mod(tmp_path, _policy_authority_mod_files())
    monkeypatch.setattr(bootstrap, "MOD_CODE", mod_dir)
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))
    repo = bootstrap.bootstrap()
    repo.create_project(
        name="policy", owner="u", seed=True, project_id="policy-authority",
    )

    ctx = repo.get_or_load("policy-authority")
    assert ctx.doc._mutation_policy_sealed is True
    assert ctx.cmd_reg.is_mutation_authority("Widget.Update") is True

    from sdk import Actor
    execution = ctx.commit.execute(
        lambda: ctx.cmd_reg.dispatch(
            "Widget.Update",
            {"id": "w0", "value": 7},
            actor=Actor(user_id="u"),
            source="user",
            doc=ctx.doc,
        )
    )
    assert execution.receipt is not None
    assert execution.receipt.status.value == "committed"
    assert ctx.doc.get("widget", "w0").data["value"] == 7


def test_open_migration_cardinality_failure_is_rejected_before_file_save(
    tmp_path, monkeypatch,
):
    import json
    import pytest

    mod_dir = _write_mod(tmp_path, _policy_overflow_migration_mod_files())
    monkeypatch.setattr(bootstrap, "MOD_CODE", mod_dir)
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))
    repo = bootstrap.bootstrap()
    proj_dir = _seed_project(bootstrap, repo, "policy-migration", widget_data={"value": 1})
    path = proj_dir / "document.json"
    before = path.read_bytes()
    before_version = json.loads(before)["_v"]

    with pytest.raises(bootstrap.ZeError) as captured:
        repo.get_or_load("policy-migration")

    assert captured.value.code == "mutation_cardinality_violation"
    assert path.read_bytes() == before
    assert json.loads(path.read_text())["_v"] == before_version
