"""W2d — copilot 引擎选取的凭据门禁(server._copilot_engine_for)。

覆盖:① 有 active CLI 绑定 → 选它、非 ambient、隔离 env;② ZE_SERVER_MODE 硬门禁下
无绑定 → fail-closed(与 copilot_allow_ambient 无关);③ 非 server_mode + ambient 允许
→ 兜底跑并留审计;④ ambient 关 → fail-closed;⑤ needs_reauth 绑定被跳过(视同未绑)。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _d in (
    _ROOT,
    _ROOT / "01-core" / "01-data-structure" / "01-document",
    _ROOT / "01-core" / "01-data-structure" / "02-entity",
    _ROOT / "01-core" / "_protocol",
):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import server  # noqa: E402
from entity import Actor, ZeError  # noqa: E402
from sdk import CredentialStore, PROVIDER_CLAUDE_CLI, provision_cli_home  # noqa: E402


class _AuditSpy:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, event_type: str, **details):
        self.events.append((event_type, details))
        return {}


def _fake_repo(tmp_path, monkeypatch, *, allow_ambient: bool = True):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    store = CredentialStore(path=tmp_path / "credentials.json",
                            key_path=tmp_path / "credentials.key")
    audit = _AuditSpy()
    repo = SimpleNamespace(
        config={"ze.ai.copilot_allow_ambient": allow_ambient},
        credential_store=store,
        auth_audit=audit,
        tool_id="demo-tool",
    )
    monkeypatch.setattr(server, "_repo", repo)
    return store, audit


def _bind_active_claude(store, user: str = "feishu:ou_a"):
    rec = store.upsert_for_user(user, provider=PROVIDER_CLAUDE_CLI)
    home = provision_cli_home(user, PROVIDER_CLAUDE_CLI, rec.id)
    store.upsert_for_user(user, credential_id=rec.id, provider=PROVIDER_CLAUDE_CLI,
                          metadata={"home_dir": str(home)})
    return rec, home


def test_active_binding_selected_non_ambient(tmp_path, monkeypatch):
    store, audit = _fake_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("ZE_SERVER_MODE", "1")   # 有绑定时 server_mode 也不拦
    rec, home = _bind_active_claude(store, "feishu:ou_a")
    eng = server._copilot_engine_for(Actor(user_id="feishu:ou_a"))
    assert eng["ambient"] is False
    assert eng["cred_id"] == rec.id
    assert eng["engine"] == "claude"
    assert eng["env"]["CLAUDE_CONFIG_DIR"] == str(home)
    assert audit.events == []


def test_server_mode_no_binding_fails_closed_regardless_of_ambient_config(tmp_path, monkeypatch):
    store, audit = _fake_repo(tmp_path, monkeypatch, allow_ambient=True)
    monkeypatch.setenv("ZE_SERVER_MODE", "1")
    with pytest.raises(ZeError) as ei:
        server._copilot_engine_for(Actor(user_id="feishu:ou_none"))
    assert ei.value.code == "requires_credential"
    assert audit.events == []          # 硬门禁不落 ambient 审计(压根没兜底)


def test_ambient_fallback_runs_and_writes_audit_when_not_server_mode(tmp_path, monkeypatch):
    store, audit = _fake_repo(tmp_path, monkeypatch, allow_ambient=True)
    monkeypatch.delenv("ZE_SERVER_MODE", raising=False)
    eng = server._copilot_engine_for(Actor(user_id="feishu:ou_amb"))
    assert eng["ambient"] is True
    assert eng["env"] == {}
    assert eng["cred_id"] == ""
    assert len(audit.events) == 1
    event_type, details = audit.events[0]
    assert event_type == "copilot_ambient_fallback"
    assert details["actor_user_id"] == "feishu:ou_amb"
    assert details["source"] == "ai"


def test_ambient_disabled_no_binding_fails_closed(tmp_path, monkeypatch):
    store, audit = _fake_repo(tmp_path, monkeypatch, allow_ambient=False)
    monkeypatch.delenv("ZE_SERVER_MODE", raising=False)
    with pytest.raises(ZeError) as ei:
        server._copilot_engine_for(Actor(user_id="feishu:ou_x"))
    assert ei.value.code == "requires_credential"
    assert audit.events == []


def test_needs_reauth_only_binding_is_skipped_as_unbound(tmp_path, monkeypatch):
    store, audit = _fake_repo(tmp_path, monkeypatch, allow_ambient=True)
    monkeypatch.setenv("ZE_SERVER_MODE", "1")
    rec, _home = _bind_active_claude(store, "feishu:ou_r")
    store.mark_needs_reauth("feishu:ou_r", rec.id)
    # 唯一绑定翻成 needs_reauth → _copilot_engine_for 视同未绑 → server_mode fail-closed
    with pytest.raises(ZeError) as ei:
        server._copilot_engine_for(Actor(user_id="feishu:ou_r"))
    assert ei.value.code == "requires_credential"


def test_is_auth_failure_detects_auth_keywords_only():
    assert server._is_auth_failure(
        ZeError("model_call_failed", "CLI 退出码 1: Error: Unauthorized (401)"))
    assert server._is_auth_failure(
        ZeError("model_call_failed", 'please run "claude login" and try again'))
    assert not server._is_auth_failure(
        ZeError("model_call_failed", "CLI 退出码 1: rate limit exceeded"))
    assert not server._is_auth_failure(
        ZeError("validation_failed", "unauthorized"))   # 非 model_call_failed 不算
