"""producer-effect gate：所有 Command handler 都不得执行外部副作用。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import command_async_check as cac  # noqa: E402


def _run_on(src: str) -> dict:
    """把一段 mod 命令源码写进临时目录,跑门禁。"""
    tmp = Path(tempfile.mkdtemp(prefix="cac-test-"))
    (tmp / "commands_register.py").write_text(src, encoding="utf-8")
    return cac.run(tmp)


def test_build_v3_demo_mod_clean():
    """build-v3 自带 demo mod:慢任务都走 stage,native 无慢调用 → 应干净。"""
    res = cac.run()  # 默认扫 build-v3 02-mod/05-code
    assert res["ok"], f"build-v3 应过本门禁,却有违规: {res['violations']}"


def test_flags_native_calling_llm():
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.gen', label='G', kind='native', touches=['x'])\n"
        "    def _gen(args, actor, source, doc):\n"
        "        return llm_call(config, 'p')\n"
    )
    assert not res["ok"]
    v = res["violations"][0]
    assert v["kind"] == "PRODUCER_CALLS_EXTERNAL_EFFECT" and v["command"] == "x.gen"


def test_missing_kind_defaults_native_and_flags_image():
    """没写 kind = 默认 native → 直调 image_call 仍判违规。"""
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.draw', label='D', touches=['x'])\n"
        "    def _draw(args, actor, source, doc):\n"
        "        return image_call(config, 'p')\n"
    )
    assert not res["ok"]
    assert res["violations"][0]["command"] == "x.draw"


def test_stage_metadata_does_not_exempt_command_handler():
    """kind='stage' 不改变 dispatch 的同步执行语义，仍必须拒绝。"""
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.batch', label='B', kind='stage', touches=['x'])\n"
        "    def _batch(args, actor, source, doc):\n"
        "        return llm_call(config, 'p')\n"
    )
    assert not res["ok"]
    assert res["violations"][0]["command"] == "x.batch"


def test_stage_handler_not_a_command_not_flagged():
    """stage handler(非 @registry.command 装饰的普通函数)里调慢 API 不该被扫到。"""
    res = _run_on(
        "def _stage_run(ctx, args):\n"
        "    return llm_call(config, 'p')\n"
        "def register(registry):\n"
        "    @registry.command(id='x.noop', label='N', kind='native')\n"
        "    def _noop(args, actor, source, doc):\n"
        "        return None\n"
    )
    assert res["ok"], res["violations"]


def test_inline_stage_body_is_ignored_but_direct_supervisor_start_is_flagged():
    """不扫描 deferred closure 本体，但 producer 直接启动 worker 仍是副作用。"""
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.start', label='S', kind='native')\n"
        "    def _start(args, actor, source, doc):\n"
        "        def _run(ctx, a):\n"
        "            return llm_call(ctx, 'p')\n"
        "        supervisor.start(run=_run)\n"
        "        return None\n"
    )
    assert not res["ok"]
    assert "supervisor.start" in res["violations"][0]["detail"]


def test_factory_returned_handler_cannot_hide_direct_supervisor_start():
    """跨文件/工厂注册拿不到完整调用图，也必须拦住会嵌套提交的 start。"""
    res = _run_on(
        "def make_handler(registry):\n"
        "    supervisor = registry.pipeline_supervisor\n"
        "    def _start(args, actor, source, doc):\n"
        "        supervisor.start(work_list=args['work_list'])\n"
        "        return new_operation(command_id='x.start')\n"
        "    return _start\n"
        "def register(registry):\n"
        "    registry.command(id='x.start', label='S', kind='stage')(make_handler(registry))\n"
    )
    assert not res["ok"]
    assert any(v["kind"] == "MOD_DIRECT_SUPERVISOR_START" for v in res["violations"])


def test_prepare_start_is_the_allowed_single_commit_path():
    res = _run_on(
        "def make_handler(registry):\n"
        "    supervisor = registry.pipeline_supervisor\n"
        "    def _start(args, actor, source, doc):\n"
        "        mutation, state = supervisor.prepare_start(work_list=args['work_list'])\n"
        "        return mutation\n"
        "    return _start\n"
        "def register(registry):\n"
        "    registry.command(id='x.start', label='S', kind='stage')(make_handler(registry))\n"
    )
    assert res["ok"], res["violations"]


def test_unrelated_service_start_is_not_a_supervisor_violation():
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.local', label='L')\n"
        "    def _local(args, actor, source, doc):\n"
        "        cache_service.start()\n"
        "        return new_operation(command_id='x.local')\n"
    )
    assert res["ok"], res["violations"]


def test_click_app_command_not_matched():
    """Click/Typer 的 @app.command() 同名装饰器不该被当成 registry 命令(误报源)。"""
    res = _run_on(
        "def build_cli(app):\n"
        "    @app.command()\n"
        "    def gen():\n"
        "        return llm_call(config, 'p')\n"
    )
    assert res["ok"], res["violations"]


def test_attribute_call_form_detected():
    """属性形式 model.llm_call(...) 也认。"""
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.g2', label='G2', kind='native')\n"
        "    def _g2(args, actor, source, doc):\n"
        "        return model.llm_call(config, 'p')\n"
    )
    assert not res["ok"]
    assert res["violations"][0]["command"] == "x.g2"


def test_one_hop_helper_effect_is_detected():
    res = _run_on(
        "def _send():\n"
        "    return notify(config, recipient='ou_x', payload={'text':'hi'})\n"
        "def register(registry):\n"
        "    @registry.command(id='x.send', label='S')\n"
        "    def _execute(args, actor, source, doc):\n"
        "        return _send()\n"
    )
    assert not res["ok"]
    assert "helper:_send" in res["violations"][0]["detail"]


def test_explicit_commandspec_registration_is_scanned():
    res = _run_on(
        "def _export(args, actor, source, doc):\n"
        "    output.write_text('x')\n"
        "    return op\n"
        "def register(registry):\n"
        "    registry.register(CommandSpec(id='x.export', label='E', execute=_export))\n"
    )
    assert not res["ok"]
    assert res["violations"][0]["command"] == "x.export"


def test_deferred_action_closure_is_not_treated_as_producer_execution():
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.defer', label='D')\n"
        "    def _execute(args, actor, source, doc):\n"
        "        def _after(commit_id):\n"
        "            output.write_text('x')\n"
        "        return CommandMutation(operations=(op,), after_commit=(DeferredAction(id='a', name='a', run=_after),))\n"
    )
    assert res["ok"], res["violations"]


def test_str_replace_two_args_not_flagged_but_path_replace_is():
    """str.replace(old,new) 恒两参 → 非文件写;Path.replace(target) 单参仍判(反哺:时间戳误判)。"""
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.stamp', label='S', kind='native', touches=['x'])\n"
        "    def _stamp(args, actor, source, doc):\n"
        "        ts = args['iso'].replace(':', '-')\n"
        "        return ts\n"
    )
    assert res["ok"], f"str.replace 双参不应判违规: {res['violations']}"
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.mv', label='M', kind='native', touches=['x'])\n"
        "    def _mv(args, actor, source, doc):\n"
        "        target_path.replace(dst_path)\n"
    )
    assert not res["ok"], "Path.replace 单参应仍判文件写违规"
