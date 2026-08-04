"""AI.Effect.Write.Check 门禁的红/绿/警告样本 + 当前树干净基线。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_effect_write_check as g  # noqa: E402


def _run_on(src: str) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="ai-eff-test-"))
    (tmp / "commands_register.py").write_text(src, encoding="utf-8")
    return g.run(tmp)


def test_build_v3_tree_is_clean():
    """当前树(builtin + 模板 mod):写文件都在 StageSpec.run/artifact 协议内 → 0 error 0 warn。"""
    res = g.run()
    assert res["ok"], f"当前树应过本门禁,却有 error: {res['errors']}"
    assert res["warning_count"] == 0, f"当前树不应有声明卫生 WARN: {res['warnings']}"


def test_ai_callable_producer_open_write_flagged():
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.w', label='W')\n"
        "    def _w(args, actor, source, doc):\n"
        "        open('/tmp/x', 'w').write('h')\n"
        "        return op\n"
    )
    assert not res["ok"]
    assert res["errors"][0]["kind"] == "AI_CALLABLE_PRODUCER_WRITES_FS"
    assert res["errors"][0]["command"] == "x.w"


def test_ai_callable_producer_open_write_via_mode_kwarg_flagged():
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.wk', label='WK')\n"
        "    def _wk(args, actor, source, doc):\n"
        "        f = open(pathvar, mode='a')\n"
        "        return op\n"
    )
    assert not res["ok"]
    assert res["errors"][0]["command"] == "x.wk"


def test_ai_callable_producer_subprocess_flagged():
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.s', label='S')\n"
        "    def _s(args, actor, source, doc):\n"
        "        subprocess.run(['ls'])\n"
        "        return op\n"
    )
    assert not res["ok"]
    assert res["errors"][0]["command"] == "x.s"


def test_one_hop_helper_path_open_write_and_commandspec_form():
    res = _run_on(
        "def _wr(p):\n"
        "    p.open('wb').write(b'x')\n"
        "def _exp(args, actor, source, doc):\n"
        "    _wr(pathobj)\n"
        "    return op\n"
        "def register(registry):\n"
        "    registry.register(CommandSpec(id='x.exp', label='E', execute=_exp))\n"
    )
    assert not res["ok"]
    v = res["errors"][0]
    assert v["command"] == "x.exp"
    assert "helper:_wr" in v["detail"]


def test_non_ai_sources_allowed_is_not_an_error():
    """显式收窄 sources_allowed(去掉 ai)→ 不再 ai-callable → 写文件不判 error(声明已显式=无 C warn)。"""
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.u', label='U', sources_allowed=('user', 'system'))\n"
        "    def _u(args, actor, source, doc):\n"
        "        open('/tmp/x', 'w').write('h')\n"
        "        return op\n"
    )
    assert res["ok"], res["errors"]
    assert res["warning_count"] == 0


def test_read_open_not_flagged():
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.r', label='R')\n"
        "    def _r(args, actor, source, doc):\n"
        "        a = open('/tmp/x').read()\n"
        "        b = open('/tmp/y', 'r').read()\n"
        "        return op\n"
    )
    assert res["ok"], res["errors"]


def test_non_file_open_with_url_not_false_flagged():
    """webbrowser.open('http://www...') 首参是长 URL(含 'w'),不是 mode 串 → 不误报。"""
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.br', label='B')\n"
        "    def _br(args, actor, source, doc):\n"
        "        webbrowser.open('http://www.example.com')\n"
        "        return op\n"
    )
    assert res["ok"], res["errors"]


def test_stage_body_not_a_producer_is_not_scanned():
    """StageSpec.run(非 @registry.command 装饰的普通函数)里写文件不该被判(artifact 协议合法落点)。"""
    res = _run_on(
        "def _stage_run(ctx, args):\n"
        "    open(args['_artifacts'][0], 'w').write('payload')\n"
        "def register(registry):\n"
        "    @registry.command(id='x.noop', label='N')\n"
        "    def _noop(args, actor, source, doc):\n"
        "        return None\n"
    )
    assert res["ok"], res["errors"]


def test_deferred_closure_write_not_treated_as_producer():
    res = _run_on(
        "def register(registry):\n"
        "    @registry.command(id='x.defer', label='D')\n"
        "    def _execute(args, actor, source, doc):\n"
        "        def _after(commit_id):\n"
        "            open('/tmp/x', 'w').write('x')\n"
        "        return CommandMutation(operations=(op,), after_commit=(DeferredAction(id='a', name='a', run=_after),))\n"
    )
    assert res["ok"], res["errors"]


def test_c_lint_warns_mutation_authority_without_declared_sources():
    res = _run_on(
        "def _m(args, actor, source, doc):\n"
        "    return op\n"
        "def register(registry):\n"
        "    registry.register(CommandSpec(id='x.m', label='M', execute=_m, mutation_authority=True))\n"
    )
    assert res["ok"]                                    # C 只 WARN,不 fail
    assert res["warning_count"] == 1
    assert res["warnings"][0]["kind"] == "UNDECLARED_SOURCES_ALLOWED"
    assert res["warnings"][0]["command"] == "x.m"


def test_c_lint_silent_when_sources_declared():
    res = _run_on(
        "def _m(args, actor, source, doc):\n"
        "    return op\n"
        "def register(registry):\n"
        "    registry.register(CommandSpec(id='x.m', label='M', execute=_m,\n"
        "        mutation_authority=True, sources_allowed=('user',)))\n"
    )
    assert res["ok"]
    assert res["warning_count"] == 0
