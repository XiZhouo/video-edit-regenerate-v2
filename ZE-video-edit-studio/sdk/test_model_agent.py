"""agent_chat — 浮层 AI copilot 的 agentic 引擎(不打真实 CLI/网络:monkeypatch subprocess)。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdk import agent_chat, ZeError  # noqa: E402
import sdk.model as model  # noqa: E402


def _fake_run(stdout: str, *, returncode: int = 0, stderr: str = ""):
    captured: dict = {}

    def run(args, **kw):
        captured["args"] = list(args)
        captured["input"] = kw.get("input")
        captured["env"] = kw.get("env")
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return run, captured


def test_claude_agent_chat_builds_mcp_and_extracts_reply(monkeypatch):
    run, cap = _fake_run(json.dumps({"type": "result", "is_error": False, "result": "做完了"}))
    monkeypatch.setattr(model.subprocess, "run", run)
    out = agent_chat(
        engine="claude", binary="claude", prompt="加个实体",
        mcp_url="http://127.0.0.1:8000/mcp?project=p1", mcp_token="tok123",
        system="sys", env={"CLAUDE_CONFIG_DIR": "/home/u/claude"},
    )
    assert out == {"reply": "做完了", "engine": "claude"}
    args = cap["args"]
    assert args[0] == "claude" and "-p" in args
    # sandbox-away-from-FS + only ze MCP tools + no host skills/MCP leakage
    assert "--strict-mcp-config" in args
    assert "--disable-slash-commands" in args
    assert args[args.index("--allowedTools") + 1] == "mcp__ze"
    cfg = json.loads(args[args.index("--mcp-config") + 1])
    assert cfg["mcpServers"]["ze"]["url"].endswith("project=p1")
    # token 走子进程 env(不进 argv/config JSON):header 只留 ${VAR} 引用,claude 展开
    assert cfg["mcpServers"]["ze"]["headers"]["Authorization"] == "Bearer ${MCP_BEARER_TOKEN}"
    assert "tok123" not in " ".join(args)                       # 真 token 从不进 argv(ps 看不到)
    assert cap["env"]["MCP_BEARER_TOKEN"] == "tok123"            # 真 token 只在子进程 env
    # per-user isolation env is passed to the subprocess
    assert cap["env"]["CLAUDE_CONFIG_DIR"] == "/home/u/claude"
    # prompt over stdin
    assert cap["input"] == "加个实体"


def test_claude_agent_chat_omits_auth_header_when_no_token(monkeypatch):
    run, cap = _fake_run(json.dumps({"result": "ok"}))
    monkeypatch.setattr(model.subprocess, "run", run)
    agent_chat(engine="claude", binary="claude", prompt="x",
               mcp_url="http://127.0.0.1:8000/mcp", mcp_token="")
    cfg = json.loads(cap["args"][cap["args"].index("--mcp-config") + 1])
    assert "headers" not in cfg["mcpServers"]["ze"]


def test_claude_agent_chat_propagates_error_envelope(monkeypatch):
    run, _ = _fake_run(json.dumps({"is_error": True, "result": "boom"}))
    monkeypatch.setattr(model.subprocess, "run", run)
    with pytest.raises(ZeError) as e:
        agent_chat(engine="claude", binary="claude", prompt="x", mcp_url="http://127/mcp")
    assert e.value.code == "model_call_failed"


def test_agent_chat_nonzero_exit_raises_with_tail(monkeypatch):
    run, _ = _fake_run("banner\nreal error at end", returncode=1, stderr="")
    monkeypatch.setattr(model.subprocess, "run", run)
    with pytest.raises(ZeError) as e:
        agent_chat(engine="claude", binary="claude", prompt="x", mcp_url="http://127/mcp")
    assert "real error at end" in str(e.value)


def test_agent_chat_rejects_unknown_engine():
    with pytest.raises(ZeError) as e:
        agent_chat(engine="bard", binary="x", prompt="y", mcp_url="http://127/mcp")
    assert e.value.code == "validation_failed"


def test_agent_chat_rejects_bad_binary_and_url():
    with pytest.raises(ZeError):
        agent_chat(engine="claude", binary="claude\n", prompt="y", mcp_url="http://127/mcp")
    with pytest.raises(ZeError):
        agent_chat(engine="claude", binary="claude", prompt="y", mcp_url="")


def test_error_tail_redacts_ephemeral_token(monkeypatch):
    # if the CLI ever echoes the bearer token into stderr, it must not leak into the ZeError
    run, _ = _fake_run("connect failed with Authorization: Bearer supersecrettok",
                       returncode=1)
    monkeypatch.setattr(model.subprocess, "run", run)
    with pytest.raises(ZeError) as e:
        agent_chat(engine="claude", binary="claude", prompt="x",
                   mcp_url="http://127/mcp", mcp_token="supersecrettok")
    assert "supersecrettok" not in str(e.value)
    assert "***" in str(e.value)


def test_codex_token_goes_via_env_not_c_args(monkeypatch):
    run, cap = _fake_run('{"last_agent_message":"ok"}')
    monkeypatch.setattr(model.subprocess, "run", run)
    # codex 0.125:streamable_http(HTTP MCP)不支持字面 bearer_token → token 走子进程 env,
    # 配 bearer_token_env_var 指向它;token 绝不进 argv(ps 看不到、不落配置文件)。
    agent_chat(engine="codex", binary="codex", prompt="q",
               mcp_url="http://127/mcp", mcp_token="tok_secret")
    joined = " ".join(cap["args"])
    assert "tok_secret" not in joined                                    # token 从不进 argv
    assert cap["env"]["MCP_BEARER_TOKEN"] == "tok_secret"                # 走子进程 env
    assert 'mcp_servers.ze.bearer_token_env_var="MCP_BEARER_TOKEN"' in joined
    assert "bearer_token=" not in joined                                 # 旧字面键彻底弃用


def test_codex_url_is_toml_escaped(monkeypatch):
    run, cap = _fake_run('{"last_agent_message":"ok"}')
    monkeypatch.setattr(model.subprocess, "run", run)
    # url 仍显式 TOML 转义,防将来输入源变化时 " 破引号成 config 注入点(code-review LOW)
    agent_chat(engine="codex", binary="codex", prompt="q",
               mcp_url='http://127/mcp?x="y', mcp_token="t")
    joined = " ".join(cap["args"])
    assert 'x="y' not in joined               # raw quote never reaches the -c value
    assert 'x=\\"y' in joined                 # escaped form present


def test_codex_reply_read_from_output_last_message(monkeypatch):
    # codex 0.125 事件格式变了,旧 stdout JSONL 解析会漏 → 改读 --output-last-message 文件
    def run(args, **kw):
        i = args.index("-o")                          # codex 把最终回复写进 -o 文件
        with open(args[i + 1], "w", encoding="utf-8") as f:
            f.write("这是最终回复\n")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    monkeypatch.setattr(model.subprocess, "run", run)
    out = agent_chat(engine="codex", binary="codex", prompt="q",
                     mcp_url="http://127/mcp", mcp_token="t")
    assert out["reply"] == "这是最终回复"           # 从 -o 文件读到、去掉尾换行


def test_codex_reply_falls_back_to_stdout_when_file_empty(monkeypatch):
    # -o 文件为空(极端/旧 codex)→ 回退解析 stdout JSONL(双保险)
    run, _ = _fake_run('{"last_agent_message":"从stdout兜底"}')
    monkeypatch.setattr(model.subprocess, "run", run)   # _fake_run 不写 -o 文件
    out = agent_chat(engine="codex", binary="codex", prompt="q",
                     mcp_url="http://127/mcp", mcp_token="t")
    assert out["reply"] == "从stdout兜底"


def test_claude_agent_chat_with_images_adds_read_and_dir(monkeypatch, tmp_path):
    img = tmp_path / "ref.png"
    img.write_bytes(b"\x89PNG\r\n")
    run, cap = _fake_run(json.dumps({"result": "看到了"}))
    monkeypatch.setattr(model.subprocess, "run", run)
    agent_chat(engine="claude", binary="claude", prompt="这是什么",
               mcp_url="http://127/mcp", mcp_token="t", image_paths=[str(img)])
    args = cap["args"]
    assert args[args.index("--allowedTools") + 1] == "mcp__ze Read"   # Read only opened for images
    assert "--add-dir" in args and str(tmp_path.resolve()) in args
    assert str(img.resolve()) in cap["input"]                         # path handed to the model


def test_codex_agent_chat_with_images_passes_dash_i(monkeypatch, tmp_path):
    img = tmp_path / "ref.jpg"
    img.write_bytes(b"\xff\xd8\xff")
    run, cap = _fake_run('{"last_agent_message":"ok"}')
    monkeypatch.setattr(model.subprocess, "run", run)
    agent_chat(engine="codex", binary="codex", prompt="q",
               mcp_url="http://127/mcp", mcp_token="t", image_paths=[str(img)])
    args = cap["args"]
    assert "-i" in args and str(img.resolve()) in args


def test_agent_chat_rejects_missing_image(monkeypatch):
    run, _ = _fake_run(json.dumps({"result": "x"}))
    monkeypatch.setattr(model.subprocess, "run", run)
    with pytest.raises(ZeError) as e:
        agent_chat(engine="claude", binary="claude", prompt="x",
                   mcp_url="http://127/mcp", image_paths=["/no/such/img.png"])
    assert e.value.code == "validation_failed"


def test_codex_agent_chat_builds_exec_and_reads_last_message(monkeypatch):
    run, cap = _fake_run('{"kind":"x"}\n{"last_agent_message":"完成"}\n')
    monkeypatch.setattr(model.subprocess, "run", run)
    out = agent_chat(
        engine="codex", binary="codex", prompt="q",
        mcp_url="http://127.0.0.1:8000/mcp?project=p1", mcp_token="t",
    )
    assert out == {"reply": "完成", "engine": "codex"}
    a = cap["args"]
    assert a[:2] == ["codex", "exec"]
    assert "--sandbox" in a and a[a.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in a
