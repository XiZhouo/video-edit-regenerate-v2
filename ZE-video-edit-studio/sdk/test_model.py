"""sdk/test_model.py — 模型调用薄驱动单测(深化点 #1).

不打真实网络/CLI:gateway 走 monkeypatch `_post_json`,claude/codex 走 monkeypatch
`subprocess.run`,图片走 monkeypatch `_post_json`。验证路线分发 / system 前置 / retry /
image_call 端点选择(generations vs edits)。
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sdk import (  # noqa: E402
    Actor, CredentialStore, PROVIDER_CLAUDE_CLI, PROVIDER_CODEX_CLI,
    PROVIDER_CURSOR_API, PROVIDER_HUPO_GATEWAY,
    PolicyStore, MODEL_SCOPE_IMAGE, MODEL_SCOPE_LLM,
    llm_call, image_call, ZeError,
)
import sdk.model as model  # noqa: E402


class FakeConfig:
    """最小 ConfigRegistry 替身:dict + get(key, fallback)。"""

    def __init__(self, **overrides):
        self._d = {
            "ze.llm.route": "gateway",
            "ze.llm.model": "",
            "ze.llm.gateway_base": "https://gw.example/v1",
            "ze.llm.gateway_key": "sk-test",
            "ze.llm.codex_account": "",
            "ze.llm.codex_bin": "codex",
            "ze.llm.claude_bin": "claude",
            "ze.image.service_url": "http://img.example:12018",
            "ze.image.model": "gemini31flash",
        }
        self._d.update(overrides)

    def get(self, key, fallback=None):
        return self._d.get(key, fallback)


# ---- LLM 路线分发 ----

def test_llm_gateway_builds_messages_and_returns_content(monkeypatch):
    seen = {}

    def fake_post(url, body, timeout, headers=None):
        seen["url"] = url
        seen["body"] = body
        seen["headers"] = headers
        return {"choices": [{"message": {"content": "网关回答"}}]}

    monkeypatch.setattr(model, "_post_json", fake_post)
    out = llm_call(FakeConfig(), "用户提问", system="你是助手", model="m1")
    assert out == "网关回答"
    assert seen["url"] == "https://gw.example/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert seen["body"]["model"] == "m1"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "你是助手"}
    assert seen["body"]["messages"][1]["content"] == "用户提问"


def test_llm_gateway_default_model_when_empty(monkeypatch):
    seen = {}
    monkeypatch.setattr(model, "_post_json",
                        lambda *a, **k: seen.update(body=a[1]) or {"choices": [{"message": {"content": "x"}}]})
    llm_call(FakeConfig(), "q")  # model 留空 → 路线缺省 claude-opus-4-8
    assert seen["body"]["model"] == "claude-opus-4-8"


def test_llm_claude_cli_prepends_system_via_stdin(monkeypatch):
    import subprocess as sp
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["input"] = kw.get("input")
        return sp.CompletedProcess(argv, 0, stdout="cli 回答\n", stderr="")

    monkeypatch.setattr(model.subprocess, "run", fake_run)
    out = llm_call(FakeConfig(**{"ze.llm.route": "claude-cli"}), "提问", system="系统")
    assert out == "cli 回答"
    assert seen["argv"][0] == "claude" and "-p" in seen["argv"]
    assert seen["input"].startswith("系统\n\n提问")


def test_llm_codex_cli_passes_account_and_model(monkeypatch):
    import subprocess as sp
    seen = {}
    monkeypatch.setattr(model.subprocess, "run",
                        lambda argv, **kw: seen.update(argv=argv) or sp.CompletedProcess(argv, 0, stdout="ok", stderr=""))
    llm_call(FakeConfig(**{"ze.llm.route": "codex-cli", "ze.llm.codex_account": "acct", "ze.llm.model": "gpt-x"}), "q")
    assert seen["argv"][:2] == ["codex", "exec"]
    assert "--skip-git-repo-check" in seen["argv"]   # 非 git 部署目录下 codex exec 必须带,否则退出码 1
    assert "--account" in seen["argv"] and "acct" in seen["argv"]
    assert "-m" in seen["argv"] and "gpt-x" in seen["argv"]


def test_llm_gateway_prefers_actor_bound_credential(monkeypatch, tmp_path):
    store = CredentialStore(path=tmp_path / "credentials.json", key_path=tmp_path / "credentials.key")
    store.upsert_for_user(
        "feishu:ou_user",
        provider=PROVIDER_HUPO_GATEWAY,
        label="user gateway",
        secret="sk-user",
        metadata={"base_url": "https://user-gw.example/v1"},
    )
    seen = {}

    def fake_post(url, body, timeout, headers=None):
        seen["url"] = url
        seen["headers"] = headers
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(model, "_post_json", fake_post)
    # P5 SSRF 护栏:自定义网关 host 出站前解析核验;测试注入公网返回(代表合法自定义端点,避免真 DNS)
    monkeypatch.setattr(model, "_AI_HOST_RESOLVER", lambda name: [(2, 1, 6, "", ("93.184.216.34", 0))])
    out = llm_call(
        FakeConfig(**{"ze.llm.gateway_key": "sk-global"}),
        "q",
        actor=Actor(user_id="feishu:ou_user"),
        credential_store=store,
    )
    assert out == "ok"
    assert seen["url"] == "https://user-gw.example/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-user"


def test_llm_gateway_requires_actor_bound_hupo_key_before_global_fallback(monkeypatch, tmp_path):
    store = CredentialStore(path=tmp_path / "credentials.json", key_path=tmp_path / "credentials.key")
    calls = {"n": 0}
    monkeypatch.setattr(model, "_post_json", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    with pytest.raises(ZeError) as ei:
        llm_call(
            FakeConfig(**{"ze.llm.gateway_key": "sk-global"}),
            "q",
            actor=Actor(user_id="feishu:ou_user"),
            credential_store=store,
        )
    assert ei.value.code == "requires_credential"
    assert calls["n"] == 0


def test_llm_gateway_allows_global_key_only_when_explicitly_enabled(monkeypatch, tmp_path):
    store = CredentialStore(path=tmp_path / "credentials.json", key_path=tmp_path / "credentials.key")
    seen = {}
    monkeypatch.setattr(
        model,
        "_post_json",
        lambda url, body, timeout, headers=None: seen.update(headers=headers) or {"choices": [{"message": {"content": "ok"}}]},
    )

    out = llm_call(
        FakeConfig(**{"ze.llm.gateway_key": "sk-global", "ze.llm.allow_global_gateway_key": True}),
        "q",
        actor=Actor(user_id="feishu:ou_user"),
        credential_store=store,
    )
    assert out == "ok"
    assert seen["headers"]["Authorization"] == "Bearer sk-global"


def test_llm_model_scope_denied_before_network(monkeypatch, tmp_path):
    policy = PolicyStore(path=tmp_path / "policies.json")
    policy.set_model_scope("feishu:ou_user", "ZE-tool-a", MODEL_SCOPE_LLM, False)
    calls = {"n": 0}
    monkeypatch.setattr(model, "_post_json", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    with pytest.raises(ZeError) as ei:
        llm_call(
            FakeConfig(),
            "q",
            actor=Actor(user_id="feishu:ou_user"),
            policy_store=policy,
            tool_id="ZE-tool-a",
        )
    assert ei.value.code == "model_scope_denied"
    assert calls["n"] == 0


def test_llm_codex_prefers_actor_bound_profile(monkeypatch, tmp_path):
    import subprocess as sp
    store = CredentialStore(path=tmp_path / "credentials.json", key_path=tmp_path / "credentials.key")
    store.upsert_for_user(
        "feishu:ou_user",
        provider=PROVIDER_CODEX_CLI,
        label="codex work",
        metadata={"account": "work", "bin": "codex-custom"},
    )
    seen = {}
    monkeypatch.setattr(model.subprocess, "run",
                        lambda argv, **kw: seen.update(argv=argv) or sp.CompletedProcess(argv, 0, stdout="ok", stderr=""))
    llm_call(
        FakeConfig(**{"ze.llm.route": "codex-cli", "ze.llm.codex_account": "global"}),
        "q",
        actor=Actor(user_id="feishu:ou_user"),
        credential_store=store,
    )
    # User metadata can select an account but can never replace the host executable.
    assert seen["argv"][:2] == ["codex", "exec"]
    assert "--account" in seen["argv"] and "work" in seen["argv"]


def test_llm_codex_demo_guest_falls_back_to_default_no_auth_error(monkeypatch, tmp_path):
    """demo/访客(__demo* uid)走 codex-cli:凭据查找该回落 config 缺省、绝不抛 auth_required。
    回归 _find_credential 缺 "__" 访客豁免 → store.find_for_user→_clean_owner 硬抛 auth_required,
    使 demo 终端对话(codex exec)在「免登录」访客态下必崩、根本到不了 codex。"""
    import subprocess as sp
    store = CredentialStore(path=tmp_path / "credentials.json", key_path=tmp_path / "credentials.key")
    seen = {}
    monkeypatch.setattr(model.subprocess, "run",
                        lambda argv, **kw: seen.update(argv=argv) or sp.CompletedProcess(argv, 0, stdout="ok", stderr=""))
    out = llm_call(
        FakeConfig(**{"ze.llm.route": "codex-cli"}),
        "q",
        actor=Actor(user_id="__demo_guest_abc123"),
        credential_store=store,
    )
    assert out == "ok"
    assert seen["argv"][:2] == ["codex", "exec"]   # 回落 config 缺省 codex,不带 --account
    assert "--account" not in seen["argv"]


def test_llm_gateway_demo_guest_falls_back_to_global_key(monkeypatch, tmp_path):
    """demo/访客(__demo* uid)走 gateway 路线:跳过每用户查找(否则 _clean_owner 抛 auth_required),
    落到 config 全局 gateway_key(运维信任域,与 CLI 回退同模型);gateway_key 空则得干净 requires_credential。
    回归 _resolve_gateway 漏 "__" 守卫 → __ 用户(含 __system__/__mcp__)在 gateway 必崩,且 line 259
    的全局 key 逃生门(连 allow_global_gateway_key=True 时)被查找门短路、永远到不了。"""
    store = CredentialStore(path=tmp_path / "credentials.json", key_path=tmp_path / "credentials.key")
    seen = {}
    monkeypatch.setattr(
        model, "_post_json",
        lambda url, body, timeout, headers=None: seen.update(url=url, headers=headers) or {"choices": [{"message": {"content": "ok"}}]},
    )
    out = llm_call(
        FakeConfig(**{"ze.llm.route": "gateway", "ze.llm.gateway_key": "sk-global"}),
        "q",
        actor=Actor(user_id="__demo_guest_xyz789"),
        credential_store=store,
    )
    assert out == "ok"
    assert seen["headers"]["Authorization"] == "Bearer sk-global"


def test_llm_unknown_route_raises(monkeypatch):
    with pytest.raises(ZeError) as ei:
        llm_call(FakeConfig(**{"ze.llm.route": "nope"}), "q")
    assert ei.value.code == "validation_failed"


def test_llm_cli_nonzero_exit_raises(monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(model.subprocess, "run",
                        lambda argv, **kw: sp.CompletedProcess(argv, 2, stdout="", stderr="boom"))
    with pytest.raises(ZeError) as ei:
        llm_call(FakeConfig(**{"ze.llm.route": "claude-cli"}), "q", retries=0)
    assert ei.value.code == "model_call_failed"


# ---- retry(薄:固定次数)----

def test_retry_recovers_then_succeeds(monkeypatch):
    monkeypatch.setattr(model.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise OSError("瞬时")
        return "good"

    assert model._with_retry(flaky, retries=2) == "good"
    assert calls["n"] == 2


def test_retry_exhausts_to_zeerror(monkeypatch):
    monkeypatch.setattr(model.time, "sleep", lambda *_: None)

    def always():
        raise OSError("挂")

    with pytest.raises(ZeError) as ei:
        model._with_retry(always, retries=1)
    assert ei.value.code == "model_call_failed"


def test_retry_does_not_retry_zeerror(monkeypatch):
    monkeypatch.setattr(model.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise ZeError("validation_failed", "不重试")

    with pytest.raises(ZeError):
        model._with_retry(bad, retries=3)
    assert calls["n"] == 1


# ---- 图片:端点选择 + 解码 ----

def test_image_generations_endpoint_and_decode(monkeypatch):
    import base64
    seen = {}
    png = base64.b64encode(b"PNGDATA").decode()
    monkeypatch.setattr(model, "_post_json",
                        lambda url, body, timeout, headers=None: seen.update(url=url, body=body) or {"data": [{"b64_json": png}]})
    out = image_call(FakeConfig(), "画只猫", size="1024x1024", model="flux")
    assert out == [b"PNGDATA"]
    assert seen["url"].endswith("/v1/images/generations")
    assert seen["body"]["size"] == "1024x1024" and seen["body"]["model"] == "flux"
    assert "images" not in seen["body"]


def test_image_model_scope_denied_before_network(monkeypatch, tmp_path):
    policy = PolicyStore(path=tmp_path / "policies.json")
    policy.set_model_scope("feishu:ou_user", "ZE-tool-a", MODEL_SCOPE_IMAGE, False)
    calls = {"n": 0}
    monkeypatch.setattr(model, "_post_json", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    with pytest.raises(ZeError) as ei:
        image_call(
            FakeConfig(),
            "画图",
            actor=Actor(user_id="feishu:ou_user"),
            policy_store=policy,
            tool_id="ZE-tool-a",
        )
    assert ei.value.code == "model_scope_denied"
    assert calls["n"] == 0


def test_image_edits_when_refs_present(monkeypatch):
    seen = {}
    monkeypatch.setattr(model, "_post_json",
                        lambda url, body, timeout, headers=None: seen.update(url=url, body=body) or {"data": [{"b64_json": "AAAA"}]})
    image_call(FakeConfig(), "改图", refs=[b"\x89PNG ref"])
    assert seen["url"].endswith("/v1/images/edits")
    assert seen["body"]["images"][0].startswith("data:image/png;base64,")


def test_image_empty_data_raises(monkeypatch):
    monkeypatch.setattr(model.time, "sleep", lambda *_: None)
    monkeypatch.setattr(model, "_post_json",
                        lambda *a, **k: {"data": []})
    with pytest.raises(ZeError) as ei:
        image_call(FakeConfig(), "x", retries=0)
    assert ei.value.code == "model_call_failed"


def test_image_url_branch_downloads(monkeypatch):
    monkeypatch.setattr(model, "_post_json",
                        lambda *a, **k: {"data": [{"url": "https://img.example/signed?token=abc"}]})
    monkeypatch.setattr(model, "_download", lambda url, timeout: b"IMGBYTES")
    assert image_call(FakeConfig(), "x") == [b"IMGBYTES"]


def test_download_error_wrapped_without_url(monkeypatch):
    import urllib.error
    monkeypatch.setattr(model.time, "sleep", lambda *_: None)
    monkeypatch.setattr(model, "_post_json",
                        lambda *a, **k: {"data": [{"url": "https://img.example/signed?token=SECRET"}]})

    def boom(req, timeout):
        raise urllib.error.URLError("conn refused")

    monkeypatch.setattr(model.urllib.request, "urlopen", boom)
    with pytest.raises(ZeError) as ei:
        image_call(FakeConfig(), "x", retries=0)
    assert ei.value.code == "model_call_failed"
    # 签名 url / token 不得出现在错误消息或上下文
    blob = str(ei.value) + json.dumps(getattr(ei.value, "context", {}), ensure_ascii=False)
    assert "SECRET" not in blob and "signed" not in blob


def test_to_data_uri_rejects_bare_path():
    with pytest.raises(ZeError) as ei:
        model._to_data_uri("/etc/passwd")
    assert ei.value.code == "validation_failed"


def test_to_data_uri_accepts_bytes_and_tuple_and_uris():
    assert model._to_data_uri(b"x").startswith("data:image/png;base64,")
    assert model._to_data_uri((b"x", "image/jpeg")).startswith("data:image/jpeg;base64,")
    assert model._to_data_uri("https://a/b.png") == "https://a/b.png"
    assert model._to_data_uri("data:image/png;base64,AAA") == "data:image/png;base64,AAA"


def test_cli_rejects_bad_binary(monkeypatch):
    with pytest.raises(ZeError) as ei:
        llm_call(FakeConfig(**{"ze.llm.route": "claude-cli", "ze.llm.claude_bin": "claude\nrm"}), "q")
    assert ei.value.code == "validation_failed"


def test_codex_account_rejects_bad_chars(monkeypatch):
    with pytest.raises(ZeError) as ei:
        llm_call(FakeConfig(**{"ze.llm.route": "codex-cli", "ze.llm.codex_account": "a;b|c"}), "q")
    assert ei.value.code == "validation_failed"


# ==============================================================
# llm_schema_call(F13 回收,1.100.0)——CLI 结构化输出编排
# 同风格:monkeypatch subprocess.run,不打真 CLI。
# ==============================================================

from sdk import llm_schema_call  # noqa: E402

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}},
          "required": ["a"], "additionalProperties": False}


def test_schema_call_rejects_unknown_engine():
    with pytest.raises(ZeError) as ei:
        llm_schema_call(engine="gpt", binary="x", prompt="q", output_schema=SCHEMA)
    assert ei.value.code == "validation_failed"


def test_schema_call_rejects_bad_binary():
    with pytest.raises(ZeError) as ei:
        llm_schema_call(engine="codex", binary="bad\nbin", prompt="q", output_schema=SCHEMA)
    assert ei.value.code == "validation_failed"


def test_schema_call_rejects_missing_image(tmp_path):
    with pytest.raises(ZeError) as ei:
        llm_schema_call(engine="codex", binary="codex", prompt="q", output_schema=SCHEMA,
                        image_paths=[tmp_path / "nope.png"])
    assert ei.value.code == "validation_failed"


def test_schema_call_rejects_missing_video(tmp_path):
    with pytest.raises(ZeError) as ei:
        llm_schema_call(
            engine="codex",
            binary="codex",
            prompt="q",
            output_schema=SCHEMA,
            video_paths=[tmp_path / "nope.mp4"],
            cwd=tmp_path,
        )
    assert ei.value.code == "validation_failed"


def test_codex_schema_call_video_uses_writable_isolated_workspace(monkeypatch, tmp_path):
    import subprocess as sp

    video = tmp_path / "optimizer-video-1.mp4"
    video.write_bytes(b"video")
    seen = {}

    def fake_exec(binary, args, prompt, *, timeout, env, cwd=None):
        argv = [binary, *args]
        seen["argv"] = argv
        seen["prompt"] = prompt
        seen["cwd"] = cwd
        return sp.CompletedProcess(argv, 0, stdout='{"a":"ok"}', stderr="")

    monkeypatch.setattr(model, "_codex_exec", fake_exec)
    out = llm_schema_call(
        engine="codex",
        binary="codex",
        prompt="analyze",
        output_schema=SCHEMA,
        video_paths=[video],
        cwd=tmp_path,
    )

    assert out == {"a": "ok"}
    argv = seen["argv"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("-C") + 1] == str(tmp_path.resolve())
    assert seen["cwd"] == str(tmp_path.resolve())
    assert "optimizer-video-1.mp4" in seen["prompt"]
    assert "ffprobe" in seen["prompt"] and "ffmpeg" in seen["prompt"]


def test_codex_schema_call_args_and_stdout_fallback(monkeypatch):
    import subprocess as sp
    seen = {}

    def fake_exec(binary, args, prompt, *, timeout, env, cwd=None):
        argv = [binary, *args]
        seen["argv"] = argv
        seen["input"] = prompt
        assert "OPENAI_API_KEY" not in env      # schema 无 key 时 env 密封
        # 不写 --output-last-message 文件 → 走 stdout JSON 兜底路径
        return sp.CompletedProcess(argv, 0, stdout='noise {"a": "ok"} tail', stderr="")

    monkeypatch.setattr(model, "_codex_exec", fake_exec)
    out = llm_schema_call(engine="codex", binary="codex", prompt="q", output_schema=SCHEMA,
                          model="gpt-5.5")
    assert out == {"a": "ok"}
    argv = seen["argv"]
    assert argv[:2] == ["codex", "exec"]
    assert "--output-schema" in argv and "--skip-git-repo-check" in argv
    assert "skills.enabled=false" in argv   # 宿主 skills 目录缺 frontmatter 会 crash,必须关
    assert "-m" in argv and "gpt-5.5" in argv
    assert seen["input"] == "q"


def test_codex_schema_call_prefers_output_file(monkeypatch):
    import subprocess as sp

    def fake_exec(binary, args, prompt, *, timeout, env, cwd=None):
        argv = [binary, *args]
        out_path = Path(argv[argv.index("--output-last-message") + 1])
        out_path.write_text('{"a": "from-file"}', encoding="utf-8")
        return sp.CompletedProcess(argv, 0, stdout='{"a": "from-stdout"}', stderr="")

    monkeypatch.setattr(model, "_codex_exec", fake_exec)
    out = llm_schema_call(engine="codex", binary="codex", prompt="q", output_schema=SCHEMA)
    assert out == {"a": "from-file"}


def test_codex_schema_call_nonzero_exit(monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(model, "_codex_exec",
                        lambda b, a, pr, **kw: sp.CompletedProcess([b, *a], 2, "", "boom"))
    with pytest.raises(ZeError) as ei:
        llm_schema_call(engine="codex", binary="codex", prompt="q", output_schema=SCHEMA)
    assert ei.value.code == "model_call_failed"


def test_codex_schema_call_timeout(monkeypatch):
    import subprocess as sp

    def fake_exec(binary, args, prompt, *, timeout, env, cwd=None):
        raise ZeError("model_call_timeout", f"codex exec 超时({timeout}s),进程组已终止",
                      context={"binary": binary, "timeout": timeout})

    monkeypatch.setattr(model, "_codex_exec", fake_exec)
    with pytest.raises(ZeError) as ei:
        llm_schema_call(engine="codex", binary="codex", prompt="q", output_schema=SCHEMA, timeout=1)
    assert ei.value.code == "model_call_timeout"   # 3.56:超时语义与 _post_json 对齐,不重试


def test_codex_schema_call_binary_not_found():
    # 真实路径断言:_codex_exec 的 Popen 对不存在的可执行抛 FileNotFoundError → 包 ZeError
    with pytest.raises(ZeError) as ei:
        llm_schema_call(engine="codex", binary="codex-definitely-missing-binary",
                        prompt="q", output_schema=SCHEMA)
    assert ei.value.code == "model_call_failed"


def test_codex_schema_call_non_json_output(monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(model, "_codex_exec",
                        lambda b, a, pr, **kw: sp.CompletedProcess([b, *a], 0, "not json at all", ""))
    with pytest.raises(ZeError) as ei:
        llm_schema_call(engine="codex", binary="codex", prompt="q", output_schema=SCHEMA)
    assert ei.value.code == "model_call_failed"


def test_claude_schema_call_text_only_no_cwd(monkeypatch):
    """蓝本尖角回归:文本-only 且 cwd 缺省 → 不 IndexError,cwd 落当前目录。"""
    import subprocess as sp
    seen = {}

    def fake_run(args, *, timeout, cwd, env):
        seen["argv"] = args
        seen["cwd"] = cwd
        env_out = {"is_error": False, "structured_output": {"a": "ok"}}
        return sp.CompletedProcess(args, 0, stdout=json.dumps(env_out), stderr="")

    monkeypatch.setattr(model, "_claude_run", fake_run)
    out = llm_schema_call(engine="claude", binary="claude", prompt="q", output_schema=SCHEMA)
    assert out == {"a": "ok"}
    assert seen["cwd"] == str(Path.cwd())
    assert "--json-schema" in seen["argv"] and "--no-session-persistence" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--allowedTools") + 1] == "Read"


def test_claude_schema_call_is_error_envelope(monkeypatch):
    import subprocess as sp
    env = {"is_error": True, "result": "quota exceeded"}
    monkeypatch.setattr(
        model, "_claude_run",
        lambda args, **kw: sp.CompletedProcess(args, 0, stdout=json.dumps(env), stderr=""),
    )
    with pytest.raises(ZeError) as ei:
        llm_schema_call(engine="claude", binary="claude", prompt="q", output_schema=SCHEMA)
    assert ei.value.code == "model_call_failed"


def test_claude_schema_call_result_string_fallback(monkeypatch):
    """structured_output 缺失 → 从 result 字符串再刮一层 JSON。"""
    import subprocess as sp
    env = {"is_error": False, "result": 'prefix {"a": "fb"} suffix'}
    monkeypatch.setattr(
        model, "_claude_run",
        lambda args, **kw: sp.CompletedProcess(args, 0, stdout=json.dumps(env), stderr=""),
    )
    out = llm_schema_call(engine="claude", binary="claude", prompt="q", output_schema=SCHEMA)
    assert out == {"a": "fb"}


def test_claude_schema_call_no_structured_at_all(monkeypatch):
    import subprocess as sp
    env = {"is_error": False, "result": "plain prose, no json"}
    monkeypatch.setattr(
        model, "_claude_run",
        lambda args, **kw: sp.CompletedProcess(args, 0, stdout=json.dumps(env), stderr=""),
    )
    with pytest.raises(ZeError) as ei:
        llm_schema_call(engine="claude", binary="claude", prompt="q", output_schema=SCHEMA)
    assert ei.value.code == "model_call_failed"


def test_claude_schema_call_video_allows_ffprobe_ffmpeg(monkeypatch, tmp_path):
    import subprocess as sp

    video = tmp_path / "optimizer-video-1.mp4"
    video.write_bytes(b"video")
    seen = {}

    def fake_run(args, *, timeout, cwd, env):
        seen["argv"] = args
        seen["prompt"] = args[2]
        seen["cwd"] = cwd
        env_out = {"is_error": False, "structured_output": {"a": "ok"}}
        return sp.CompletedProcess(args, 0, stdout=json.dumps(env_out), stderr="")

    monkeypatch.setattr(model, "_claude_run", fake_run)
    out = llm_schema_call(
        engine="claude",
        binary="claude",
        prompt="analyze",
        output_schema=SCHEMA,
        video_paths=[video],
        cwd=tmp_path,
    )

    assert out == {"a": "ok"}
    assert seen["cwd"] == str(tmp_path.resolve())
    assert "--add-dir" in seen["argv"]
    assert str(tmp_path.resolve()) in seen["argv"]
    assert seen["argv"][seen["argv"].index("--allowedTools") + 1] == (
        model._CLAUDE_VIDEO_ALLOWED_TOOLS
    )
    assert "Bash(ffprobe *)" in model._CLAUDE_VIDEO_ALLOWED_TOOLS
    assert "Bash(ffmpeg *)" in model._CLAUDE_VIDEO_ALLOWED_TOOLS
    assert "optimizer-video-1.mp4" in seen["prompt"]
    assert "ffprobe" in seen["prompt"] and "ffmpeg" in seen["prompt"]


def test_claude_schema_call_timeout_uses_process_group(monkeypatch):
    def boom(args, *, timeout, cwd, env):
        raise ZeError(
            "model_call_timeout",
            f"claude -p 调用超时({timeout}s),进程组已终止",
            context={"binary": args[0], "timeout": timeout},
        )

    monkeypatch.setattr(model, "_claude_run", boom)
    with pytest.raises(ZeError) as ei:
        llm_schema_call(
            engine="claude", binary="claude", prompt="q",
            output_schema=SCHEMA, timeout=1,
        )
    assert ei.value.code == "model_call_timeout"


# ---- AI 通道层(3.56.0):image_call 路由 / 密封 env / provenance / 版本 pin ----

def _route_cfg(**over):
    d = {"ze.image.route": "service", "ze.image.codex_model": "gpt-5.5"}
    d.update(over)
    return FakeConfig(**d)


def test_image_route_explicit_no_fallback(monkeypatch):
    """显式路线凭据缺失 → requires_credential,绝不静默回退到 service(裁定 1)。"""
    monkeypatch.setattr(model, "_find_credential", lambda **kw: None)
    for route in ("codex-cli", "openai-api", "gemini-api"):
        with pytest.raises(ZeError) as e:
            image_call(_route_cfg(**{"ze.image.route": route}), "p")
        assert e.value.code == "requires_credential", route


def test_image_route_unknown_rejected():
    with pytest.raises(ZeError) as e:
        image_call(_route_cfg(**{"ze.image.route": "magic"}), "p")
    assert e.value.code == "validation_failed"


def test_image_route_auto_prefers_service(monkeypatch):
    """auto:内网服务已配 → service(免费池化优先);行为=service 路线原样。"""
    calls = {}
    def fake_post(url, body, timeout, headers=None):
        calls["url"] = url
        return {"data": [{"b64_json": "aGk="}]}
    monkeypatch.setattr(model, "_post_json", fake_post)
    out = image_call(_route_cfg(**{"ze.image.route": "auto"}), "p")
    assert out == [b"hi"] and "/v1/images/generations" in calls["url"]


def test_image_route_auto_no_options(monkeypatch):
    """auto 且无服务无绑定 → requires_credential(前置提示接得住)。"""
    monkeypatch.setattr(model, "_find_credential", lambda **kw: None)
    with pytest.raises(ZeError) as e:
        image_call(_route_cfg(**{"ze.image.route": "auto", "ze.image.service_url": ""}), "p")
    assert e.value.code == "requires_credential"


def test_image_route_auto_honors_model_routes(tmp_path):
    """model_routes:所选模型决定 auto 路由——多路线用户选 GPT Image 2 落能产它的路线(codex/openai),
    选 Banana Pro 落 gemini,而非固定 provider 顺序(所选=所调)。缺映射/未给 → 回退固定顺序。
    委托 resolver 后用真 CredentialStore(credential_store 参数注入)替旧 _find_credential mock。"""
    from credentials import CredentialStore
    ROUTES = {"fal-gpt-image-2": ["service", "codex-cli", "openai-api"],
              "gemini3": ["service", "gemini-api"]}
    store = CredentialStore(path=tmp_path / "c.json", key_path=tmp_path / "c.key")
    uid = "feishu:u"
    store.upsert_for_user(uid, provider=model.PROVIDER_CODEX_CLI, label="c", secret="")   # codex+gemini 绑
    store.upsert_for_user(uid, provider=model.PROVIDER_GEMINI_API, label="g", secret="sk-g")  # openai 没绑
    cfg = _route_cfg(**{"ze.image.route": "auto", "ze.image.service_url": ""})
    act = Actor(user_id=uid)

    def pick(**kw):
        return model._pick_image_route(cfg, actor=act, credential_store=store, **kw)
    # 选 GPT Image 2:gemini 不产它(被排除)→ 落 codex,不误落 gemini 出成 Banana
    assert pick(model="fal-gpt-image-2", model_routes=ROUTES) == "codex-cli"
    # 选 Banana Pro:codex 不产它 → 落 gemini
    assert pick(model="gemini3", model_routes=ROUTES) == "gemini-api"
    # 无 model_routes → 回退固定顺序(ROUTE_PREFERENCE service>openai>gemini>codex)→ gemini 先绑
    assert pick() == "gemini-api"
    # 模型不在映射 → allowed=None → 回退固定顺序
    assert pick(model="unknown", model_routes=ROUTES) == "gemini-api"


def test_image_route_auto_model_routes_none_producible(tmp_path):
    """选了某模型但没有任何能产它的路线可用 → requires_credential(与前端置灰一致,禁执行)。"""
    from credentials import CredentialStore
    ROUTES = {"gemini3": ["service", "gemini-api"]}
    store = CredentialStore(path=tmp_path / "c.json", key_path=tmp_path / "c.key")
    uid = "feishu:u"
    store.upsert_for_user(uid, provider=model.PROVIDER_CODEX_CLI, label="c", secret="")   # 只绑 codex(产 GPT,不产 Banana)
    cfg = _route_cfg(**{"ze.image.route": "auto", "ze.image.service_url": ""})
    with pytest.raises(ZeError) as e:
        model._pick_image_route(cfg, actor=Actor(user_id=uid), credential_store=store,
                                model="gemini3", model_routes=ROUTES)   # 选 Banana 但只有 codex
    assert e.value.code == "requires_credential"


def _raise_ze(code, ctx):
    def _fn(*a, **k):
        raise ZeError(code, "x", context=ctx)
    return _fn


def test_image_auto_failover_on_unreached(monkeypatch):
    """auto:首候选连接失败(unreached=未扣费)→ 安全落下一条并成功(P6 派发期 failover)。"""
    monkeypatch.setattr(model, "_pick_image_routes_ordered", lambda *a, **k: ("openai-api", "gemini-api"))
    seen = []
    monkeypatch.setattr(model, "_image_via_openai",
                        lambda *a, **k: seen.append("openai") or _raise_ze("model_call_failed", {"unreached": True})())
    monkeypatch.setattr(model, "_image_via_gemini", lambda *a, **k: seen.append("gemini") or [b"img"])
    out = image_call(_route_cfg(**{"ze.image.route": "auto"}), "p")
    assert out == [b"img"] and seen == ["openai", "gemini"]     # failover 发生


def test_image_auto_no_failover_on_http_error(monkeypatch):
    """auto:首候选 HTTP 错误(有 status=provider 已响应,可能已扣费)→ 抛,**绝不试下一条**(不双扣费)。"""
    monkeypatch.setattr(model, "_pick_image_routes_ordered", lambda *a, **k: ("openai-api", "gemini-api"))
    seen = []
    monkeypatch.setattr(model, "_image_via_openai",
                        lambda *a, **k: seen.append("openai") or _raise_ze("model_call_failed", {"status": 500})())
    monkeypatch.setattr(model, "_image_via_gemini", lambda *a, **k: seen.append("gemini") or [b"img"])
    with pytest.raises(ZeError) as e:
        image_call(_route_cfg(**{"ze.image.route": "auto"}), "p")
    assert e.value.context.get("status") == 500 and seen == ["openai"]   # 未试 gemini


def test_image_auto_no_failover_on_timeout(monkeypatch):
    """auto:首候选超时(歧义,可能已处理/扣费)→ 抛,不 failover。"""
    monkeypatch.setattr(model, "_pick_image_routes_ordered", lambda *a, **k: ("openai-api", "gemini-api"))
    seen = []
    monkeypatch.setattr(model, "_image_via_openai",
                        lambda *a, **k: seen.append("openai") or _raise_ze("model_call_timeout", {"timeout": 600})())
    monkeypatch.setattr(model, "_image_via_gemini", lambda *a, **k: seen.append("gemini") or [b"img"])
    with pytest.raises(ZeError) as e:
        image_call(_route_cfg(**{"ze.image.route": "auto"}), "p")
    assert e.value.code == "model_call_timeout" and seen == ["openai"]


def test_image_auto_all_unreached_raises_last(monkeypatch):
    """auto:全候选连接失败 → 抛最后一条(不无限、不吞错)。"""
    monkeypatch.setattr(model, "_pick_image_routes_ordered", lambda *a, **k: ("openai-api", "gemini-api"))
    monkeypatch.setattr(model, "_image_via_openai", _raise_ze("model_call_failed", {"unreached": True}))
    monkeypatch.setattr(model, "_image_via_gemini", _raise_ze("model_call_failed", {"unreached": True}))
    with pytest.raises(ZeError) as e:
        image_call(_route_cfg(**{"ze.image.route": "auto"}), "p")
    assert e.value.code == "model_call_failed"


def test_image_pinned_no_failover_even_on_unreached(monkeypatch):
    """显式路线连接失败 → 直接抛,**不 failover**(pinned 单派发,no-silent-fallback)。"""
    seen = []
    monkeypatch.setattr(model, "_image_via_openai",
                        lambda *a, **k: seen.append("openai") or _raise_ze("model_call_failed", {"unreached": True})())
    monkeypatch.setattr(model, "_image_via_gemini", lambda *a, **k: seen.append("gemini") or [b"img"])
    with pytest.raises(ZeError):
        image_call(_route_cfg(**{"ze.image.route": "openai-api"}), "p")
    assert seen == ["openai"]                                   # 绝不落 gemini


def test_receipt_trace_auto_hit(monkeypatch):
    """§P7 写侧:auto 命中 → receipt_sink['trace'] 含候选序 + 命中路线(供消费方 record 落调用日志)。"""
    monkeypatch.setattr(model, "_pick_image_routes_ordered", lambda *a, **k: ("service", "openai-api"))
    monkeypatch.setattr(model, "_image_via_service", lambda *a, **k: [b"img"])
    sink: dict = {}
    image_call(_route_cfg(**{"ze.image.route": "auto"}), "p", receipt_sink=sink)
    assert sink["route"] == "service"
    assert sink["trace"][-1] == "命中 service"
    assert "service" in sink["trace"][0] and "openai-api" in sink["trace"][0]   # 候选序


def test_receipt_trace_records_failover(monkeypatch):
    """failover 过程写进 trace:首候选连接失败(未扣费)→ 记 failover → 命中下一条。"""
    monkeypatch.setattr(model, "_pick_image_routes_ordered", lambda *a, **k: ("openai-api", "gemini-api"))
    monkeypatch.setattr(model, "_image_via_openai", _raise_ze("model_call_failed", {"unreached": True}))
    monkeypatch.setattr(model, "_image_via_gemini", lambda *a, **k: [b"img"])
    sink: dict = {}
    image_call(_route_cfg(**{"ze.image.route": "auto"}), "p", receipt_sink=sink)
    assert any("failover" in t for t in sink["trace"])
    assert sink["trace"][-1] == "命中 gemini-api"
    assert sink["route"] == "gemini-api"                       # 回执落位=实际命中路线


def test_receipt_trace_pinned(monkeypatch):
    monkeypatch.setattr(model, "_image_via_openai", lambda *a, **k: [b"img"])
    sink: dict = {}
    image_call(_route_cfg(**{"ze.image.route": "openai-api"}), "p", receipt_sink=sink)
    assert sink["trace"] == ("pinned openai-api",)


def test_hub_resolve_image_route_differential_equivalence():
    """差分证明(永久回归门):sdk.hub.resolve_image_route(用 AccessConnection + ROUTE_PREFERENCE)与
    **旧 _pick_image_route 算法的内联参照**(固定 provider 顺序 service>openai>gemini>codex + 逐 provider
    绑定判定)在(service × 凭证子集 × model × model_routes)全矩阵上返回**同一 route**(不可用侧 None)。
    _pick_image_route 已委托 resolver(不再自比,否则循环),故这里对照内联参照——保住「resolve_image_route
    等价于旧固定顺序算法」的证据。含关键分歧样例(同连 openai+codex 无 service 选 fal):老路 openai 先 →
    openai-api;新路若按 served_by 声明序会误落 codex——ROUTE_PREFERENCE 已对齐消歧。"""
    import itertools
    from sdk import hub
    IMG_PROVIDERS = [model.PROVIDER_OPENAI_API, model.PROVIDER_GEMINI_API, model.PROVIDER_CODEX_CLI]
    ROUTES = {"fal-gpt-image-2": ["service", "codex-cli", "openai-api"],
              "gemini3": ["service", "gemini-api"]}

    def old_pick(bound, m, mr, service_configured):
        """委托前 _pick_image_route 的逐字参照:固定 provider 顺序 + 绑定判定。"""
        allowed = None
        if m and isinstance(mr, dict):
            spec = mr.get(m)
            if isinstance(spec, (list, tuple)):
                allowed = {str(x) for x in spec}
            elif isinstance(spec, str):
                allowed = {spec}

        def ok(route):
            return allowed is None or route in allowed
        if ok("service") and service_configured:
            return "service"
        for provider, route in ((model.PROVIDER_OPENAI_API, "openai-api"),
                                (model.PROVIDER_GEMINI_API, "gemini-api"),
                                (model.PROVIDER_CODEX_CLI, "codex-cli")):
            if ok(route) and provider in bound:
                return route
        return None

    def conns_for(bound):
        return hub.project_connections([
            {"id": f"c-{p}", "provider": p, "capabilities": ["llm", "image"],
             "status": "active", "metadata": {}, "last4": "", "has_secret": True}
            for p in bound
        ])

    checked = 0
    for r in range(len(IMG_PROVIDERS) + 1):
        for bound in itertools.combinations(IMG_PROVIDERS, r):
            bound = set(bound)
            conns = conns_for(bound)
            for svc in ("", "http://svc"):
                for m in ("fal-gpt-image-2", "gemini3", None, "unknown"):
                    for mr in (ROUTES, None):
                        old = old_pick(bound, m, mr, bool(svc))
                        new = hub.resolve_image_route(
                            m, conns, service_configured=bool(svc),
                            model_routes=mr, deploy_audience=hub.AUDIENCE_INTERNAL)
                        assert old == new, (
                            f"分歧 bound={sorted(bound)} svc={bool(svc)} model={m} "
                            f"routes={'MAP' if mr else None}: old={old} new={new}")
                        checked += 1
    assert checked == 2 ** len(IMG_PROVIDERS) * 2 * 4 * 2   # 全矩阵覆盖(128 组)


def test_projection_matches_find_credential_wiring(tmp_path):
    """image_call 委托 resolver 的**接线**证明(补差分 mock 缺口的最后一块):
    project_connections(guarded store.list_for_user) 的每 provider 可用性 == _find_credential(store,
    capability='image') 判定,对真 CredentialStore 多态(active / needs_reauth)+ 访客('__' 前缀)。
    与既有差分(resolve ≡ _pick_image_route,给定同可用性)合起来 ⇒ image_call 换 resolve_image_route
    是行为保持的。委托接线须复刻此处的访客门:uid 空/以 '__' 开头 → 不查 store(否则 find_for_user
    对 __demo* 硬抛 auth_required)、视作无连接。"""
    from credentials import CredentialStore
    from sdk import hub

    store = CredentialStore(path=tmp_path / "c.json", key_path=tmp_path / "c.key")
    uid = "feishu:u1"
    store.upsert_for_user(uid, provider=model.PROVIDER_OPENAI_API, label="o", secret="sk-o")
    store.upsert_for_user(uid, provider=model.PROVIDER_GEMINI_API, label="g", secret="sk-g")
    store.upsert_for_user(uid, provider=model.PROVIDER_CODEX_CLI, label="c", secret="")
    codex_id = [r.id for r in store.list_for_user(uid) if r.provider == model.PROVIDER_CODEX_CLI][0]
    store.mark_needs_reauth(uid, codex_id)   # 失效态:两侧都应判不可用

    provider_route = {model.PROVIDER_OPENAI_API: "openai-api",
                      model.PROVIDER_GEMINI_API: "gemini-api",
                      model.PROVIDER_CODEX_CLI: "codex-cli"}

    def guarded_conns(u):
        if not u or u.startswith("__"):
            return ()
        return hub.project_connections([r.public_dict() for r in store.list_for_user(u)])

    for actor_uid in (uid, "__demo_guest_x"):
        conns = guarded_conns(actor_uid)
        for prov, route in provider_route.items():
            fc = model._find_credential(providers=(prov,), actor=Actor(user_id=actor_uid),
                                        credential_store=store, capability="image") is not None
            r = hub.resolve_image_route("m", conns, service_configured=False,
                                        model_routes={"m": [route]}, deploy_audience=hub.AUDIENCE_INTERNAL)
            assert fc == (r == route), (
                f"wiring mismatch uid={actor_uid} prov={prov}: find_cred={fc} resolve_avail={r == route}")


def test_sealed_cli_env_never_leaks_openai_key(monkeypatch):
    """致命点2 机械化:白名单 env 绝不带宿主 OPENAI_API_KEY(否则订阅静默切 API 计费)。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = model._sealed_cli_env({"CODEX_HOME": "/tmp/h"})
    assert "OPENAI_API_KEY" not in env
    assert env["CODEX_HOME"] == "/tmp/h" and env["PATH"] == "/usr/bin"


class _FakeRec:
    id = "cred-1"
    metadata = {"home_dir": "/tmp/ze-test-codex-home"}


def _bind_fake_codex(monkeypatch):
    monkeypatch.setattr(model, "_find_credential", lambda **kw: _FakeRec())
    monkeypatch.setattr(model, "_credential_runtime", lambda a, s: (None, None))
    monkeypatch.setattr(model, "_actor_user_id", lambda a: "u1")


def test_codex_image_version_pin_blocks(monkeypatch):
    """版本 pin:允许范围外硬禁路线(未实证版本可能改落盘契约→静默产 0 图;0.140.0 无实证仍禁)。"""
    _bind_fake_codex(monkeypatch)
    monkeypatch.setattr(model, "_codex_cli_version", lambda b: "0.140.0")
    with pytest.raises(ZeError) as e:
        image_call(_route_cfg(**{"ze.image.route": "codex-cli"}), "p")
    assert "0.140.0" in str(e.value)


def test_codex_image_provenance_gate(monkeypatch, tmp_path):
    """致命点1 机械化:事件流没有原生 image_gen 调用 → 拒绝采信任何 PNG 产物。"""
    _bind_fake_codex(monkeypatch)
    monkeypatch.setattr(model, "_codex_cli_version", lambda b: "0.125.0")
    import subprocess as sp
    def fake_exec(binary, args, prompt, *, timeout, env, cwd=None):
        assert "OPENAI_API_KEY" not in env          # 密封 env 全链断言
        # 伪装退化:写一张"合法 PNG"到 cwd,但事件流无工具调用标识
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 5000
        (Path(cwd) / "out.png").write_bytes(png)
        return sp.CompletedProcess([binary], 0, '{"type":"agent_message"}', "")
    monkeypatch.setattr(model, "_codex_exec", fake_exec)
    with pytest.raises(ZeError) as e:
        image_call(_route_cfg(**{"ze.image.route": "codex-cli"}), "p")
    assert "原生图像生成工具" in str(e.value)


def test_codex_image_happy_path_with_provenance(monkeypatch):
    """provenance 在场 → 取 ABS_OUT 产物;计费路线单次调用(无 retry 包装)。"""
    _bind_fake_codex(monkeypatch)
    monkeypatch.setattr(model, "_codex_cli_version", lambda b: "0.125.0")
    import subprocess as sp
    calls = {"n": 0}
    def fake_exec(binary, args, prompt, *, timeout, env, cwd=None):
        calls["n"] += 1
        png = b"\x89PNG\r\n\x1a\n" + b"1" * 5000
        (Path(cwd) / "out.png").write_bytes(png)
        return sp.CompletedProcess([binary], 0,
                                   '{"type":"tool_call","name":"image_gen"}', "")
    monkeypatch.setattr(model, "_codex_exec", fake_exec)
    out = image_call(_route_cfg(**{"ze.image.route": "codex-cli"}), "p")
    assert len(out) == 1 and out[0][:8] == b"\x89PNG\r\n\x1a\n"
    assert calls["n"] == 1


def test_capability_union_migration():
    """读侧并集迁移:存量 codex 绑定(快照无 image)读出自动含 image,无需重绑。"""
    import credentials as cr
    caps = cr._clean_capabilities(cr.PROVIDER_CODEX_CLI, ["llm", "agent"])
    assert "image" in caps and "llm" in caps and "agent" in caps
    caps2 = cr._clean_capabilities(cr.PROVIDER_GEMINI_API, None)
    assert caps2 == ("llm", "image")


def test_gemini_route_parses_inline_data(monkeypatch):
    monkeypatch.setattr(model, "_resolve_api_key", lambda p, **kw: "k")
    monkeypatch.setattr(model, "_post_json",
                        lambda url, body, timeout, headers=None:
                        {"candidates": [{"content": {"parts": [
                            {"inlineData": {"data": "aGk="}}]}}]})
    out = image_call(_route_cfg(**{"ze.image.route": "gemini-api"}), "p")
    assert out == [b"hi"]


def test_user_schema_call_passes_bound_codex_home(monkeypatch, tmp_path):
    store = CredentialStore(path=tmp_path / "c.json", key_path=tmp_path / "c.key")
    uid = "feishu:codex-user"
    store.upsert_for_user(
        uid,
        provider=PROVIDER_CODEX_CLI,
        metadata={"home_dir": str(tmp_path / "codex-home")},
    )
    captured = {}

    def fake_schema_call(**kwargs):
        captured.update(kwargs)
        return {"optimized_prompt": "ok"}

    monkeypatch.setattr(model, "llm_schema_call", fake_schema_call)
    result = model.user_llm_schema_call(
        provider=PROVIDER_CODEX_CLI,
        prompt="p",
        output_schema={"type": "object"},
        config=FakeConfig(),
        actor=Actor(user_id=uid),
        credential_store=store,
    )

    assert result == {"optimized_prompt": "ok"}
    assert captured["engine"] == "codex"
    assert captured["env"] == {"CODEX_HOME": str((tmp_path / "codex-home").resolve())}


def test_user_schema_call_forwards_managed_video_to_codex(monkeypatch, tmp_path):
    store = CredentialStore(path=tmp_path / "c.json", key_path=tmp_path / "c.key")
    uid = "feishu:codex-video-user"
    store.upsert_for_user(
        uid,
        provider=PROVIDER_CODEX_CLI,
        metadata={"home_dir": str(tmp_path / "codex-home")},
    )
    work = tmp_path / "managed-work"
    work.mkdir()
    video = work / "optimizer-video-1.mp4"
    video.write_bytes(b"video")
    captured = {}

    def fake_schema_call(**kwargs):
        captured.update(kwargs)
        return {"optimized_prompt": "ok"}

    monkeypatch.setattr(model, "llm_schema_call", fake_schema_call)
    result = model.user_llm_schema_call(
        provider=PROVIDER_CODEX_CLI,
        prompt="p",
        output_schema={"type": "object"},
        video_paths=[video],
        cwd=work,
        config=FakeConfig(),
        actor=Actor(user_id=uid),
        credential_store=store,
    )

    assert result == {"optimized_prompt": "ok"}
    assert captured["video_paths"] == [video.resolve()]
    assert captured["cwd"] == work


def test_user_schema_call_forwards_managed_video_to_claude(monkeypatch, tmp_path):
    store = CredentialStore(path=tmp_path / "c.json", key_path=tmp_path / "c.key")
    uid = "feishu:claude-video-user"
    store.upsert_for_user(
        uid,
        provider=PROVIDER_CLAUDE_CLI,
        metadata={"home_dir": str(tmp_path / "claude-home")},
    )
    work = tmp_path / "managed-work"
    work.mkdir()
    video = work / "optimizer-video-1.mp4"
    video.write_bytes(b"video")
    captured = {}

    def fake_schema_call(**kwargs):
        captured.update(kwargs)
        return {"optimized_prompt": "ok"}

    monkeypatch.setattr(model, "llm_schema_call", fake_schema_call)
    result = model.user_llm_schema_call(
        provider=PROVIDER_CLAUDE_CLI,
        prompt="p",
        output_schema={"type": "object"},
        video_paths=[video],
        cwd=work,
        config=FakeConfig(),
        actor=Actor(user_id=uid),
        credential_store=store,
    )

    assert result == {"optimized_prompt": "ok"}
    assert captured["engine"] == "claude"
    assert captured["video_paths"] == [video.resolve()]
    assert captured["cwd"] == work
    assert captured["env"] == {
        "CLAUDE_CONFIG_DIR": str((tmp_path / "claude-home").resolve()),
    }


def test_user_schema_call_passes_bound_claude_home(monkeypatch, tmp_path):
    store = CredentialStore(path=tmp_path / "c.json", key_path=tmp_path / "c.key")
    uid = "feishu:claude-user"
    store.upsert_for_user(
        uid,
        provider=PROVIDER_CLAUDE_CLI,
        metadata={"home_dir": str(tmp_path / "claude-home")},
    )
    captured = {}

    def fake_schema_call(**kwargs):
        captured.update(kwargs)
        return {"optimized_prompt": "ok"}

    monkeypatch.setattr(model, "llm_schema_call", fake_schema_call)
    result = model.user_llm_schema_call(
        provider=PROVIDER_CLAUDE_CLI,
        prompt="p",
        output_schema={"type": "object"},
        config=FakeConfig(),
        actor=Actor(user_id=uid),
        credential_store=store,
    )

    assert result == {"optimized_prompt": "ok"}
    assert captured["engine"] == "claude"
    assert captured["env"] == {
        "CLAUDE_CONFIG_DIR": str((tmp_path / "claude-home").resolve()),
    }


def test_user_schema_call_passes_only_selected_cursor_secret(monkeypatch, tmp_path):
    store = CredentialStore(path=tmp_path / "c.json", key_path=tmp_path / "c.key")
    store.upsert_for_user("feishu:a", provider=PROVIDER_CURSOR_API, secret="cursor-key-a")
    store.upsert_for_user("feishu:b", provider=PROVIDER_CURSOR_API, secret="cursor-key-b")
    seen = []

    def fake_cursor(**kwargs):
        seen.append(kwargs)
        return {"optimized_prompt": "safe"}

    monkeypatch.setattr(model, "_cursor_sdk_schema_call", fake_cursor)
    for uid, expected in (("feishu:a", "cursor-key-a"), ("feishu:b", "cursor-key-b")):
        result = model.user_llm_schema_call(
            provider="cursor",
            prompt="p",
            output_schema={"type": "object"},
            model="grok-4.5",
            actor=Actor(user_id=uid),
            credential_store=store,
        )
        assert result["optimized_prompt"] == "safe"
        assert seen[-1]["api_key"] == expected

    assert "cursor-key-a" not in json.dumps(store.list_for_user("feishu:a")[0].public_dict())


def test_concurrent_cursor_users_never_cross_keys(monkeypatch, tmp_path):
    store = CredentialStore(path=tmp_path / "c.json", key_path=tmp_path / "c.key")
    store.upsert_for_user("feishu:a", provider=PROVIDER_CURSOR_API, secret="cursor-key-a")
    store.upsert_for_user("feishu:b", provider=PROVIDER_CURSOR_API, secret="cursor-key-b")

    def fake_cursor(**kwargs):
        return {"optimized_prompt": kwargs["api_key"], "warnings": [], "facts_used": []}

    monkeypatch.setattr(model, "_cursor_sdk_schema_call", fake_cursor)

    def run(uid):
        return model.user_llm_schema_call(
            provider="cursor", prompt="p", output_schema={"type": "object"},
            model="grok-4.5", actor=Actor(user_id=uid), credential_store=store,
        )["optimized_prompt"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("feishu:a", "feishu:b")))
    assert results == ["cursor-key-a", "cursor-key-b"]


def test_cursor_sdk_key_is_per_call_not_env_and_workspace_is_cleaned(monkeypatch):
    import cursor_sdk

    captured = {}
    workspace = {}

    class _Result:
        status = "finished"
        result = '{"optimized_prompt":"ok","warnings":[],"facts_used":[]}'
        id = "run-safe"

    class _Run:
        id = "run-safe"

        def wait(self):
            return _Result()

        def cancel(self):
            captured["cancelled"] = True

    class _Agent:
        agent_id = "agent-safe"

        def __init__(self, options):
            captured["key"] = options.api_key
            captured["env"] = dict(model.os.environ)
            workspace["path"] = Path(options.local.cwd)
            assert workspace["path"].is_dir()
            assert options.local.setting_sources == ()
            assert options.local.sandbox_options.enabled is True
            assert options.local.store["type"] == "sqlite"
            assert Path(options.local.store["root_dir"]).is_dir()

        def send(self, message):
            captured["message"] = message
            return _Run()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(cursor_sdk.Agent, "create", staticmethod(lambda options: _Agent(options)))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    payload = model._cursor_sdk_schema_call(
        prompt="p",
        output_schema={"type": "object"},
        api_key="cursor-secret-per-call",
        model="grok-4.5",
        image_paths=[],
        timeout=30,
    )

    assert payload["optimized_prompt"] == "ok"
    assert captured["key"] == "cursor-secret-per-call"
    assert captured["closed"] is True
    assert "cursor-secret-per-call" not in json.dumps(captured["env"])
    assert not workspace["path"].exists()


def test_cursor_sdk_copies_videos_into_workspace(monkeypatch, tmp_path):
    import cursor_sdk

    source = tmp_path / "optimizer-video-1.mp4"
    source.write_bytes(b"guided-video")
    captured = {}

    class _Result:
        status = "finished"
        result = '{"optimized_prompt":"ok","warnings":[],"facts_used":[]}'
        id = "run-video"

    class _Run:
        id = "run-video"

        def wait(self):
            return _Result()

        def cancel(self):
            pass

    class _Agent:
        agent_id = "agent-video"

        def __init__(self, options):
            cwd = Path(options.local.cwd)
            copied = cwd / "optimizer-video-1.mp4"
            assert copied.is_file()
            assert copied.read_bytes() == b"guided-video"
            captured["cwd"] = cwd

        def send(self, message):
            captured["message"] = message
            return _Run()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(cursor_sdk.Agent, "create", staticmethod(lambda options: _Agent(options)))
    payload = model._cursor_sdk_schema_call(
        prompt="inspect the guided video",
        output_schema={"type": "object"},
        api_key="cursor-secret",
        model="grok-4.5",
        image_paths=[],
        video_paths=[source],
        timeout=30,
    )

    assert payload["optimized_prompt"] == "ok"
    assert "optimizer-video-1.mp4" in captured["message"]
    assert "ffprobe" in captured["message"]
    assert captured["closed"] is True
    assert not captured["cwd"].exists()


def test_user_schema_call_forwards_managed_video_to_cursor(monkeypatch, tmp_path):
    store = CredentialStore(path=tmp_path / "c.json", key_path=tmp_path / "c.key")
    uid = "feishu:cursor-video-user"
    store.upsert_for_user(uid, provider=PROVIDER_CURSOR_API, secret="cursor-key")
    work = tmp_path / "managed-work"
    work.mkdir()
    video = work / "optimizer-video-1.mp4"
    video.write_bytes(b"video")
    captured = {}

    def fake_cursor(**kwargs):
        captured.update(kwargs)
        return {"optimized_prompt": "ok"}

    monkeypatch.setattr(model, "_cursor_sdk_schema_call", fake_cursor)
    result = model.user_llm_schema_call(
        provider=PROVIDER_CURSOR_API,
        prompt="p",
        output_schema={"type": "object"},
        model="grok-4.5",
        video_paths=[video],
        actor=Actor(user_id=uid),
        credential_store=store,
    )

    assert result == {"optimized_prompt": "ok"}
    assert captured["video_paths"] == [video.resolve()]
    assert captured["api_key"] == "cursor-key"


def test_cursor_sdk_timeout_cancels_run(monkeypatch):
    import cursor_sdk

    events = []

    class _Run:
        id = "run-slow"

        def wait(self):
            model.time.sleep(2)
            return type("R", (), {"status": "finished", "result": "{}", "id": "run-slow"})()

        def cancel(self):
            events.append("cancel")

    class _Agent:
        agent_id = "agent-slow"

        def send(self, _message):
            return _Run()

        def close(self):
            events.append("close")

    monkeypatch.setattr(
        cursor_sdk.Agent, "create",
        staticmethod(lambda options: _Agent()),
    )
    with pytest.raises(ZeError) as exc:
        model._cursor_sdk_schema_call(
            prompt="p", output_schema={"type": "object"},
            api_key="cursor-timeout-secret", model="grok-4.5",
            image_paths=[], timeout=1,
        )
    assert exc.value.code == "model_call_timeout"
    assert "cancel" in events and "close" in events
    assert "cursor-timeout-secret" not in str(exc.value)


def test_cursor_sdk_cancel_event_aborts_run(monkeypatch):
    import cursor_sdk

    events = []
    cancel_event = model.threading.Event()

    class _Run:
        id = "run-cancel"

        def wait(self):
            model.time.sleep(2)
            return type("R", (), {"status": "finished", "result": "{}", "id": "run-cancel"})()

        def cancel(self):
            events.append("cancel")

    class _Agent:
        agent_id = "agent-cancel"

        def send(self, _message):
            cancel_event.set()
            return _Run()

        def close(self):
            events.append("close")

    monkeypatch.setattr(cursor_sdk.Agent, "create", staticmethod(lambda options: _Agent()))
    with pytest.raises(ZeError) as exc:
        model._cursor_sdk_schema_call(
            prompt="p", output_schema={"type": "object"},
            api_key="cursor-cancel-secret", model="grok-4.5",
            image_paths=[], timeout=30, cancel_event=cancel_event,
        )
    assert exc.value.code == "cancelled"
    assert "cancel" in events and "close" in events


def test_cursor_sdk_errors_redact_api_key(monkeypatch):
    import cursor_sdk

    class _Agent:
        agent_id = "agent-fail"

        def send(self, _message):
            raise RuntimeError("transport failed for cursor-super-secret")

        def close(self):
            pass

    monkeypatch.setattr(cursor_sdk.Agent, "create", staticmethod(lambda options: _Agent()))
    with pytest.raises(ZeError) as exc:
        model._cursor_sdk_schema_call(
            prompt="p", output_schema={"type": "object"},
            api_key="cursor-super-secret", model="grok-4.5", image_paths=[],
        )
    assert "cursor-super-secret" not in str(exc.value)
    assert "REDACTED" in str(exc.value)


def test_cursor_model_catalog_uses_current_users_key(monkeypatch, tmp_path):
    import cursor_sdk

    store = CredentialStore(path=tmp_path / "c.json", key_path=tmp_path / "c.key")
    store.upsert_for_user("feishu:a", provider=PROVIDER_CURSOR_API, secret="cursor-key-a")
    seen = []

    class _Row:
        id = "grok-4.5"
        display_name = "Grok 4.5"
        description = "Fast reasoning"

    def fake_list(*, api_key):
        seen.append(api_key)
        return [_Row()]

    monkeypatch.setattr(cursor_sdk.Cursor.models, "list", fake_list)
    rows = model.cursor_models_for_current_user(
        actor=Actor(user_id="feishu:a"), credential_store=store,
    )

    assert seen == ["cursor-key-a"]
    assert rows == [{
        "id": "grok-4.5",
        "display_name": "Grok 4.5",
        "description": "Fast reasoning",
    }]
    assert "cursor-key-a" not in json.dumps(rows)


def test_cursor_model_catalog_errors_redact_key(monkeypatch, tmp_path):
    import cursor_sdk

    store = CredentialStore(path=tmp_path / "c.json", key_path=tmp_path / "c.key")
    store.upsert_for_user(
        "feishu:a", provider=PROVIDER_CURSOR_API, secret="cursor-catalog-secret",
    )

    def fail(*, api_key):
        raise RuntimeError(f"catalog failed for {api_key}")

    monkeypatch.setattr(cursor_sdk.Cursor.models, "list", fail)
    with pytest.raises(ZeError) as exc:
        model.cursor_models_for_current_user(
            actor=Actor(user_id="feishu:a"), credential_store=store,
        )

    assert "cursor-catalog-secret" not in str(exc.value)
    assert "REDACTED" in str(exc.value)
