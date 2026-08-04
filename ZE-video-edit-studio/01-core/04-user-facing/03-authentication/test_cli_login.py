"""test_cli_login.py — 设备登录管理器(Codex 全链路 mock;Claude PTY 走真跑验证不在此)。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sdk  # noqa: F401,E402  触发叶子路径注入
import cli_login  # noqa: E402
from credentials import CredentialStore  # noqa: E402
from entity import ZeError  # noqa: E402

_CODEX_LINES = [
    "\nWelcome to Codex\n",
    "Follow these steps to sign in with ChatGPT using device code authorization:\n",
    "1. Open this link in your browser and sign in to your account\n",
    "   https://auth.openai.com/codex/device\n",
    "2. Enter this one-time code (expires in 15 minutes)\n",
    "   U87F-0UWSR\n",
]


class _FakeProc:
    def __init__(self, lines, home):
        self._lines = list(lines)
        self._home = home
        self.stdout = iter(self._lines)   # reader thread 迭代
        self.returncode = None
        self._done = False                # test 置 True = 模拟用户批准 + CLI 完成

    def poll(self):
        if self._done:
            os.makedirs(self._home, exist_ok=True)
            with open(os.path.join(self._home, "auth.json"), "w") as f:
                json.dump({"auth_mode": "chatgpt", "tokens": {"account_id": "ACC123456"}}, f)
            self.returncode = 0
            return 0
        return None

    def terminate(self): pass
    def wait(self, timeout=None): return 0
    def kill(self): pass


def _store(tmp):
    return CredentialStore(path=tmp / "credentials.json", key_path=tmp / "credentials.key")


def test_codex_device_login_full_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    store = _store(tmp_path)
    ref = {}

    def fake_popen(args, env=None, **kw):
        p = _FakeProc(_CODEX_LINES, env["CODEX_HOME"])
        ref["p"] = p
        return p

    monkeypatch.setattr(cli_login.subprocess, "Popen", fake_popen)
    mgr = cli_login.CliLoginManager()

    # start → 抓到固定授权链接 + 一次性验证码,等用户批准
    r = mgr.start("feishu:ou_alice", "codex", codex_bin="codex")
    assert r["status"] == cli_login.STATUS_PENDING
    assert r["verify_url"] == "https://auth.openai.com/codex/device"
    assert r["user_code"] == "U87F-0UWSR"
    assert r["provider"] == "codex"

    # 用户还没批准 → 仍 pending
    assert mgr.poll("feishu:ou_alice", r["login_id"], store)["status"] == cli_login.STATUS_PENDING

    # 模拟用户在浏览器批准 + CLI 写好 auth.json
    ref["p"]._done = True
    r2 = mgr.poll("feishu:ou_alice", r["login_id"], store)
    assert r2["status"] == cli_login.STATUS_SUCCESS

    # 建了一条正式 codex 绑定,凭据落进隔离目录
    rows = store.list_for_user("feishu:ou_alice")
    assert len(rows) == 1 and rows[0].provider == "codex_cli_profile"
    home = rows[0].metadata["home_dir"]
    assert json.load(open(os.path.join(home, "auth.json")))["tokens"]["account_id"] == "ACC123456"
    assert "ACC123" in rows[0].label   # 自动起名带账号标识


def test_codex_process_early_exit_is_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))

    class _DeadProc(_FakeProc):
        def poll(self): self.returncode = 1; return 1   # 一起来就退出=失败

    def fake_popen(args, env=None, **kw):
        return _DeadProc([], env["CODEX_HOME"])

    monkeypatch.setattr(cli_login.subprocess, "Popen", fake_popen)
    mgr = cli_login.CliLoginManager()
    with pytest.raises(ZeError):
        mgr.start("feishu:ou_bob", "codex", codex_bin="codex")


def test_start_rejects_bad_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    mgr = cli_login.CliLoginManager()
    with pytest.raises(ZeError) as e:
        mgr.start("feishu:ou_x", "openai", codex_bin="codex")
    assert e.value.code == "validation_failed"


def test_safe_tail_redacts_device_code_and_url_query():
    # 诊断尾巴回显前必须抹掉设备验证码 + 授权 URL 的敏感 query string(可能含 token/一次性码)
    raw = ("visit https://auth.openai.com/codex/device?token=SEKRET123&x=1\n"
           "enter code U87F-0UWSR to continue")
    out = cli_login._safe_tail(raw)
    assert "U87F-0UWSR" not in out and "[code]" in out          # 设备码脱敏
    assert "token=SEKRET123" not in out and "[redacted]" in out  # URL query 脱敏
    assert "auth.openai.com/codex/device" in out                # 非敏感部分保留(可诊断)


def test_shutdown_all_cleans_live_sessions(tmp_path, monkeypatch):
    # 登录进行中恰逢 server 关停:子进程被杀、PTY/临时目录清理、会话清空(防 amber redeploy 泄漏)
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    killed = {"n": 0}

    class _Live(_FakeProc):
        def terminate(self): killed["n"] += 1
        def poll(self): return None   # 一直活着,不完成

    def fake_popen(args, env=None, **kw):
        return _Live(_CODEX_LINES, env["CODEX_HOME"])

    monkeypatch.setattr(cli_login.subprocess, "Popen", fake_popen)
    mgr = cli_login.CliLoginManager()
    r = mgr.start("feishu:ou_a", "codex", codex_bin="codex")
    tmp_home = mgr._sessions[r["login_id"]].tmp_home
    assert os.path.isdir(tmp_home)

    mgr.shutdown_all()
    assert mgr._sessions == {}            # 会话清空
    assert killed["n"] >= 1               # 子进程被终止
    assert not os.path.isdir(tmp_home)    # 临时目录删除


def test_extract_claude_url_dewraps():
    # TUI 把长 URL 硬折行,提取要去空白拼回
    buf = ("Browser didn't open? Use the url below\n\n"
           "https://claude.com/cai/oauth/authorize?code=true&client_id=abc\n"
           "def&response_type=code&scope=user\n\nPaste code here >")
    url = cli_login.CliLoginManager._extract_claude_url(buf)
    assert url.startswith("https://claude.com/cai/oauth/authorize?code=true")
    assert "\n" not in url and " " not in url
    assert "client_id=abcdef" in url   # 折断处拼回
