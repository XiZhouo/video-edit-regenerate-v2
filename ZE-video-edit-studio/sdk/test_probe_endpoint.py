"""POST /api/account/credentials/{id}/probe 集成测试(ProviderHub 回收 P3b)。

覆盖:①openai 凭据探活 → reveal secret + 打官方 host + 返 live 健康;②官方 CLI 凭据 →
not_applicable **且不解密、不外发**(最小暴露);③未知 credential_id → not_found。
mock hub.probe_url 免真网络。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _d in (_ROOT, _ROOT / "01-core" / "01-data-structure" / "01-document",
           _ROOT / "01-core" / "01-data-structure" / "02-entity",
           _ROOT / "01-core" / "_protocol"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import server  # noqa: E402
import sdk.hub as hub_mod  # noqa: E402
from entity import Actor, ZeError  # noqa: E402
from sdk import CredentialStore, PROVIDER_OPENAI_API, PROVIDER_CLAUDE_CLI  # noqa: E402
from sdk.hub.probe import ProbeResult  # noqa: E402
from sdk.hub import DiscoveredModelStore  # noqa: E402


class _FakeReq:
    def __init__(self, body): self._body = body
    async def json(self): return self._body


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    store = CredentialStore(path=tmp_path / "credentials.json", key_path=tmp_path / "credentials.key")
    disc = DiscoveredModelStore(tmp_path / "discovered.json")
    repo = SimpleNamespace(credential_store=store, discovered_model_store=disc, config={}, tool_id="demo-tool")
    monkeypatch.setattr(server, "_repo", repo)
    monkeypatch.setattr(server, "_require_tool_access", lambda actor: None)  # 非本测重点
    calls = []
    monkeypatch.setattr(hub_mod, "probe_url",
                        lambda url, headers=None, timeout=15: calls.append((url, headers))
                        or ProbeResult(ok=True, health="ok", detail="HTTP 200", latency_ms=12))
    # reveal_secret spy(验最小暴露:not_applicable 分支不该解密)
    reveals = []
    orig_reveal = store.reveal_secret
    monkeypatch.setattr(store, "reveal_secret",
                        lambda uid, cid: reveals.append(cid) or orig_reveal(uid, cid))
    return store, calls, reveals


def test_openai_probe_reveals_and_hits_official_host(harness):
    store, calls, reveals = harness
    rec = store.upsert_for_user("feishu:ou_a", provider=PROVIDER_OPENAI_API, secret="sk-test")
    out = server.account_credentials_probe(rec.id, Actor(user_id="feishu:ou_a"))
    assert out["ok"] and out["probe"]["health"] == "ok" and out["probe"]["latency_ms"] == 12
    # 打的是官方 host + Bearer(SSRF 白名单内),secret 已 reveal
    url, headers = calls[0]
    assert url == "https://api.openai.com/v1/models"
    assert headers == {"Authorization": "Bearer sk-test"}
    assert reveals == [rec.id]


def test_official_cli_not_applicable_no_reveal_no_outbound(harness):
    store, calls, reveals = harness
    rec = store.upsert_for_user("feishu:ou_a", provider=PROVIDER_CLAUDE_CLI)
    out = server.account_credentials_probe(rec.id, Actor(user_id="feishu:ou_a"))
    assert out["probe"]["health"] == "not_applicable"
    assert calls == []          # 未外发
    assert reveals == []        # 未解密(最小暴露:is_probeable 门在 reveal 前)


def test_unknown_credential_id_not_found(harness):
    with pytest.raises(ZeError) as ei:
        server.account_credentials_probe("nope", Actor(user_id="feishu:ou_a"))
    assert ei.value.code == "not_found"


# ── /api/ai/discover(P4b)────────────────────────────────────────────────────
def test_discover_records_layered_discovered(harness, monkeypatch):
    store, calls, reveals = harness
    rec = store.upsert_for_user("feishu:ou_a", provider=PROVIDER_OPENAI_API, secret="sk-x")
    monkeypatch.setattr(hub_mod, "discover_models",
                        lambda url, headers=None, kind="": (["gpt-4o", "o3"], None))
    out = asyncio.run(server.ai_discover_models(_FakeReq({"credential_id": rec.id}),
                                                Actor(user_id="feishu:ou_a")))
    assert out["ok"] and out["count"] == 2
    # 教义①:落库条目 source=discovered/caps=[]
    assert all(e["source"] == "discovered" and e["caps"] == [] for e in out["discovered"])
    assert reveals == [rec.id]
    # 已入独立分层 store(不进 canonical)
    got = server._repo.discovered_model_store.list_for_credential("feishu:ou_a", rec.id)
    assert [e["model_id"] for e in got] == ["gpt-4o", "o3"]


def test_discover_official_cli_empty_no_reveal(harness, monkeypatch):
    store, calls, reveals = harness
    rec = store.upsert_for_user("feishu:ou_a", provider=PROVIDER_CLAUDE_CLI)
    out = asyncio.run(server.ai_discover_models(_FakeReq({"credential_id": rec.id}),
                                                Actor(user_id="feishu:ou_a")))
    assert out["discovered"] == [] and reveals == []       # 官方 CLI 不解密


def test_discover_provider_error_not_recorded(harness, monkeypatch):
    store, calls, reveals = harness
    rec = store.upsert_for_user("feishu:ou_a", provider=PROVIDER_OPENAI_API, secret="sk-x")
    monkeypatch.setattr(hub_mod, "discover_models",
                        lambda url, headers=None, kind="": ([], "credential_expired"))
    out = asyncio.run(server.ai_discover_models(_FakeReq({"credential_id": rec.id}),
                                                Actor(user_id="feishu:ou_a")))
    assert out["ok"] is False and out["error_code"] == "credential_expired"
    # 发现失败不落库(不写脏数据)
    assert server._repo.discovered_model_store.list_for_credential("feishu:ou_a", rec.id) == []
