from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdk import (  # noqa: E402
    CredentialStore,
    PROVIDER_HUPO_GATEWAY,
    PROVIDER_CODEX_CLI,
    PROVIDER_CLAUDE_CLI,
    PROVIDER_CURSOR_API,
    ai_homes_dir,
    provision_cli_home,
    write_cli_auth_file,
    cli_home_env,
    remove_cli_home,
    META_HOME_DIR,
    ZeError,
)


def _store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(path=tmp_path / "credentials.json", key_path=tmp_path / "credentials.key")


def test_store_seals_secret_and_public_view_hides_owner(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_1",
        provider=PROVIDER_HUPO_GATEWAY,
        label="我的网关",
        secret="sk-user-secret",
        metadata={"base_url": "https://gw.example/v1"},
    )

    raw = (tmp_path / "credentials.json").read_text(encoding="utf-8")
    assert "sk-user-secret" not in raw
    assert store.reveal_secret("feishu:ou_1", rec.id) == "sk-user-secret"

    public = store.list_for_user("feishu:ou_1")[0].public_dict()
    assert public["has_secret"] is True
    assert public["last4"] == "cret"
    assert "owner_user_id" not in public
    assert "secret_ref" not in public


def test_cursor_api_key_is_sealed_owned_and_llm_capable(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_cursor",
        provider=PROVIDER_CURSOR_API,
        label="My Cursor",
        secret="cursor-user-secret",
    )

    raw = (tmp_path / "credentials.json").read_text(encoding="utf-8")
    assert "cursor-user-secret" not in raw
    assert store.reveal_secret("feishu:ou_cursor", rec.id) == "cursor-user-secret"
    assert set(rec.capabilities) == {"llm", "agent"}
    assert rec.status == "active"
    assert store.list_for_user("feishu:other") == []


def test_public_view_hides_server_side_cli_home(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_1",
        provider=PROVIDER_CODEX_CLI,
        metadata={
            "home_dir": "/srv/private/ai-homes/alice",
            "internal_token": "never-public",
            "account": "visible",
        },
    )

    public = rec.public_dict()

    assert public["metadata"] == {"account": "visible"}
    assert "/srv/private" not in json.dumps(public)
    assert "never-public" not in json.dumps(public)


def test_store_scopes_credentials_to_owner(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user("feishu:ou_1", provider=PROVIDER_CODEX_CLI, metadata={"account": "a"})
    assert store.list_for_user("feishu:ou_2") == []
    with pytest.raises(ZeError) as ei:
        store.get_for_user("feishu:ou_2", rec.id)
    assert ei.value.code == "not_found"


def test_find_for_user_filters_provider_and_capability(tmp_path):
    store = _store(tmp_path)
    store.upsert_for_user("feishu:ou_1", provider=PROVIDER_CODEX_CLI, metadata={"account": "acct"})
    rec = store.find_for_user(
        "feishu:ou_1",
        providers=(PROVIDER_CODEX_CLI,),
        capability="llm",
    )
    assert rec is not None
    assert rec.metadata["account"] == "acct"


def test_delete_removes_record_and_secret(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user("feishu:ou_1", provider=PROVIDER_HUPO_GATEWAY, secret="sk-delete")
    assert store.delete_for_user("feishu:ou_1", rec.id) is True
    raw = json.loads((tmp_path / "credentials.json").read_text(encoding="utf-8"))
    assert raw["credentials"] == []
    assert raw["secrets"] == {}


# ---- Per-user CLI account isolation (agentic AI runner) --------------------

def test_provision_cli_home_creates_isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    home = provision_cli_home("feishu:ou_alice", PROVIDER_CLAUDE_CLI, "cred_abc12345")
    assert home.is_dir()
    assert home.parent.name == PROVIDER_CLAUDE_CLI
    assert home.name == "cred_abc12345"
    assert ai_homes_dir() in home.parents
    # 0700 so only the running unix user can traverse the token dir.
    assert (home.stat().st_mode & 0o777) == 0o700


def test_provision_cli_home_distinct_users_never_collide(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    a = provision_cli_home("feishu:ou_x", PROVIDER_CODEX_CLI, "cred_1")
    b = provision_cli_home("feishu_ou_x", PROVIDER_CODEX_CLI, "cred_1")  # sanitizes to same prefix
    assert a != b  # hash suffix keeps them apart


def test_provision_cli_home_rejects_api_key_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    with pytest.raises(ZeError) as e:
        provision_cli_home("feishu:ou_x", PROVIDER_HUPO_GATEWAY, "cred_1")
    assert e.value.code == "validation_failed"


def test_write_cli_auth_file_writes_json_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    home = provision_cli_home("feishu:ou_x", PROVIDER_CLAUDE_CLI, "cred_1")
    blob = json.dumps({"claudeAiOauth": {"accessToken": "tok", "refreshToken": "r", "expiresAt": 1}})
    target = write_cli_auth_file(home, PROVIDER_CLAUDE_CLI, blob)
    assert target.name == ".credentials.json"
    assert (target.stat().st_mode & 0o777) == 0o600
    assert json.loads(target.read_text())["claudeAiOauth"]["accessToken"] == "tok"

    codex_home = provision_cli_home("feishu:ou_x", PROVIDER_CODEX_CLI, "cred_2")
    ct = write_cli_auth_file(codex_home, PROVIDER_CODEX_CLI, json.dumps({"auth_mode": "chatgpt"}))
    assert ct.name == "auth.json"


def test_write_cli_auth_file_rejects_non_json(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    home = provision_cli_home("feishu:ou_x", PROVIDER_CLAUDE_CLI, "cred_1")
    with pytest.raises(ZeError) as e:
        write_cli_auth_file(home, PROVIDER_CLAUDE_CLI, "not-json{{")
    assert e.value.code == "validation_failed"


def test_cli_home_env_maps_provider_to_env_var(tmp_path):
    h = str(tmp_path.resolve())  # already-canonical path (avoid /tmp→/private/tmp symlink)
    assert cli_home_env(PROVIDER_CLAUDE_CLI, h) == {"CLAUDE_CONFIG_DIR": h}
    assert cli_home_env(PROVIDER_CODEX_CLI, h) == {"CODEX_HOME": h}
    assert cli_home_env(PROVIDER_HUPO_GATEWAY, h) == {}


# ---- W2d 缺口 B:吊销连删磁盘目录(refresh token 不留盘) ----

def test_delete_for_user_wipes_only_this_cred_home_and_keeps_owner_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    store = _store(tmp_path)
    # 同一 owner 绑两条 CLI 账号,各有隔离 home + refresh blob
    rec_a = store.upsert_for_user("feishu:ou_alice", provider=PROVIDER_CLAUDE_CLI)
    rec_b = store.upsert_for_user("feishu:ou_alice", provider=PROVIDER_CODEX_CLI)
    home_a = provision_cli_home("feishu:ou_alice", PROVIDER_CLAUDE_CLI, rec_a.id)
    home_b = provision_cli_home("feishu:ou_alice", PROVIDER_CODEX_CLI, rec_b.id)
    write_cli_auth_file(home_a, PROVIDER_CLAUDE_CLI,
                        json.dumps({"claudeAiOauth": {"refreshToken": "secret-a"}}))
    write_cli_auth_file(home_b, PROVIDER_CODEX_CLI, json.dumps({"auth_mode": "chatgpt"}))
    store.upsert_for_user("feishu:ou_alice", credential_id=rec_a.id,
                          provider=PROVIDER_CLAUDE_CLI, metadata={META_HOME_DIR: str(home_a)})

    owner_root = home_a.parent.parent            # .../ai-homes/<owner>
    assert home_a.exists() and (home_a / ".credentials.json").exists()

    assert store.delete_for_user("feishu:ou_alice", rec_a.id) is True

    assert not home_a.exists()                   # 该 cred 目录 + refresh token 已抹除
    assert home_b.exists()                       # 同 owner 的另一条不受影响
    assert owner_root.exists()                   # owner 根仍在


def test_delete_for_user_tolerates_already_absent_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    store = _store(tmp_path)
    rec = store.upsert_for_user("feishu:ou_x", provider=PROVIDER_CLAUDE_CLI)
    # home 目录从未 provision(metadata 无 home_dir → 走 canonical 回退,路径不存在),delete 不应抛
    assert store.delete_for_user("feishu:ou_x", rec.id) is True


def test_remove_cli_home_never_deletes_owner_root_or_outside(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    home = provision_cli_home("feishu:ou_z", PROVIDER_CLAUDE_CLI, "cred_11112222")
    owner_root = home.parent.parent
    provider_dir = home.parent
    # 传 owner 根 / provider 目录 / 越界路径:都必须拒删
    remove_cli_home("feishu:ou_z", PROVIDER_CLAUDE_CLI, "cred_11112222", str(owner_root))
    remove_cli_home("feishu:ou_z", PROVIDER_CLAUDE_CLI, "cred_11112222", str(provider_dir))
    outside = tmp_path / "outside"
    outside.mkdir()
    remove_cli_home("feishu:ou_z", PROVIDER_CLAUDE_CLI, "cred_11112222", str(outside))
    assert owner_root.exists() and provider_dir.exists() and outside.exists()
    # 传该 cred 的真 home:删掉
    remove_cli_home("feishu:ou_z", PROVIDER_CLAUDE_CLI, "cred_11112222", str(home))
    assert not home.exists()
    assert owner_root.exists()


# ---- W2d 缺口 C:令牌失效 → needs_reauth 态 ----

def test_mark_needs_reauth_flips_cli_binding_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user("feishu:ou_a", provider=PROVIDER_CLAUDE_CLI)
    assert rec.status == "active"
    assert store.mark_needs_reauth("feishu:ou_a", rec.id) is True
    assert store.get_for_user("feishu:ou_a", rec.id).status == "needs_reauth"
    # 幂等:已是 needs_reauth 再调返回 False,不重复写
    assert store.mark_needs_reauth("feishu:ou_a", rec.id) is False


def test_mark_needs_reauth_ignores_api_key_and_foreign_owner(tmp_path):
    store = _store(tmp_path)
    api = store.upsert_for_user("feishu:ou_a", provider=PROVIDER_HUPO_GATEWAY, secret="sk-x")
    assert store.mark_needs_reauth("feishu:ou_a", api.id) is False   # 非 CLI provider
    cli = store.upsert_for_user("feishu:ou_a", provider=PROVIDER_CODEX_CLI)
    assert store.mark_needs_reauth("feishu:ou_b", cli.id) is False   # 越权 owner 不动


def test_reauth_via_upsert_same_cred_restores_active(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user("feishu:ou_a", provider=PROVIDER_CLAUDE_CLI)
    store.mark_needs_reauth("feishu:ou_a", rec.id)
    # 同 cred_id 再 upsert(设备登录 _finalize 的幂等覆盖路径)复位为 active
    store.upsert_for_user("feishu:ou_a", credential_id=rec.id, provider=PROVIDER_CLAUDE_CLI)
    assert store.get_for_user("feishu:ou_a", rec.id).status == "active"
