"""Cursor 凭据分流：只写本工具私有库，绝不污染 Hub credentials.json。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "01-core" / "04-user-facing" / "03-authentication"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "02-mod" / "05-code"))
sys.path.insert(0, str(AUTH))

from credentials import (  # noqa: E402
    PROVIDER_CODEX_CLI,
    PROVIDER_CURSOR_API,
    CredentialStore,
)
import cursor_credentials as cc  # noqa: E402


@pytest.fixture()
def isolated_dirs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    auth = tmp_path / "auth"
    data.mkdir()
    auth.mkdir()
    monkeypatch.setenv("ZE_DATA_DIR", str(data))
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(auth))
    cc.reset_cursor_credential_store_for_tests()
    yield {"data": data, "auth": auth, "hub": auth / "credentials.json"}
    cc.reset_cursor_credential_store_for_tests()


def test_cursor_upsert_stays_in_local_store(isolated_dirs):
    hub = CredentialStore(
        path=isolated_dirs["hub"],
        key_path=isolated_dirs["auth"] / "credentials.key",
    )
    store = cc.store_for_provider(PROVIDER_CURSOR_API, hub)
    assert store.path == cc.cursor_credentials_path()
    store.upsert_for_user(
        "user_test",
        provider=PROVIDER_CURSOR_API,
        label="cursor",
        secret="crsr_test_secret_value_aaa",
    )

    assert cc.cursor_credentials_path().is_file()
    local = json.loads(cc.cursor_credentials_path().read_text(encoding="utf-8"))
    providers = {r.get("provider") for r in local.get("credentials", [])}
    assert PROVIDER_CURSOR_API in providers

    hub_raw = json.loads(isolated_dirs["hub"].read_text(encoding="utf-8"))
    hub_providers = {r.get("provider") for r in hub_raw.get("credentials", [])}
    assert PROVIDER_CURSOR_API not in hub_providers
    # Hub secrets 也不得出现 cursor 相关 secret_ref
    for rec in hub_raw.get("credentials", []):
        assert rec.get("provider") != PROVIDER_CURSOR_API


def test_codex_still_writes_hub(isolated_dirs):
    hub = CredentialStore(
        path=isolated_dirs["hub"],
        key_path=isolated_dirs["auth"] / "credentials.key",
    )
    store = cc.store_for_provider(PROVIDER_CODEX_CLI, hub)
    assert store.path == hub.path
    store.upsert_for_user(
        "user_test",
        provider=PROVIDER_CODEX_CLI,
        label="codex",
        secret="",
    )
    hub_raw = json.loads(isolated_dirs["hub"].read_text(encoding="utf-8"))
    providers = {r.get("provider") for r in hub_raw.get("credentials", [])}
    assert PROVIDER_CODEX_CLI in providers
    assert not cc.cursor_credentials_path().exists() or PROVIDER_CURSOR_API not in {
        r.get("provider")
        for r in json.loads(cc.cursor_credentials_path().read_text(encoding="utf-8")).get(
            "credentials", []
        )
    }


def test_list_merged_hides_hub_cursor_and_shows_local(isolated_dirs):
    hub = CredentialStore(
        path=isolated_dirs["hub"],
        key_path=isolated_dirs["auth"] / "credentials.key",
    )
    # 模拟 Hub 里残留的旧 cursor（列表应过滤掉）
    hub.upsert_for_user(
        "user_test",
        provider=PROVIDER_CURSOR_API,
        label="legacy-hub-cursor",
        secret="crsr_legacy_hub",
    )
    local = cc.cursor_credential_store()
    local.upsert_for_user(
        "user_test",
        provider=PROVIDER_CURSOR_API,
        label="tool-cursor",
        secret="crsr_local_only",
    )
    hub.upsert_for_user(
        "user_test",
        provider=PROVIDER_CODEX_CLI,
        label="codex",
        secret="",
    )

    merged = cc.list_merged_for_user(hub, "user_test")
    by_provider = {}
    for rec in merged:
        by_provider.setdefault(rec.provider, []).append(rec.label)
    assert by_provider[PROVIDER_CODEX_CLI] == ["codex"]
    assert by_provider[PROVIDER_CURSOR_API] == ["tool-cursor"]
    assert "legacy-hub-cursor" not in by_provider.get(PROVIDER_CURSOR_API, [])


def test_resolve_prefers_cursor_store(isolated_dirs):
    hub = CredentialStore(
        path=isolated_dirs["hub"],
        key_path=isolated_dirs["auth"] / "credentials.key",
    )
    local = cc.cursor_credential_store()
    rec = local.upsert_for_user(
        "user_test",
        provider=PROVIDER_CURSOR_API,
        secret="crsr_resolve_me",
    )
    store, found = cc.resolve_store_for_credential(hub, "user_test", rec.id)
    assert store.path == local.path
    assert found.id == rec.id
