"""Trust-boundary contracts for global config and user credential REST writes."""
from __future__ import annotations

from sdk.test_bootstrap_projects import _auth, _load_app, _run_scenario


def test_global_config_requires_admin_and_mandatory_cas(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)
    monkeypatch.setenv("ZE_SUPER_ADMIN_USERS", "alice")

    async def steps(client):
        # This is a managed core test and must only use a framework-owned key.
        # A Build V3 demo key would make every downstream without that demo
        # configuration fail the otherwise target-independent admin contract.
        config_key = "ze.ai.governance_enabled"
        tokens = {}
        for user in ("alice", "bob"):
            response = await client.post(
                "/api/auth/register",
                json={"username": user, "password": f"{user}-pass-1"},
            )
            tokens[user] = response.json()["token"]
        client.cookies.clear()

        snapshot = await client.get("/api/config", headers=_auth(tokens["alice"]))
        assert snapshot.status_code == 200
        version = snapshot.json()["version"]

        denied = await client.put(
            "/api/config",
            json={"updates": {config_key: True}, "expected_version": version},
            headers={**_auth(tokens["bob"]), "Idempotency-Key": "cfg-bob-1"},
        )
        assert denied.status_code == 403

        missing_cas = await client.put(
            "/api/config",
            json={"updates": {config_key: True}},
            headers={**_auth(tokens["alice"]), "Idempotency-Key": "cfg-admin-missing"},
        )
        assert missing_cas.status_code == 422

        saved = await client.put(
            "/api/config",
            json={"updates": {config_key: True}, "expected_version": version},
            headers={**_auth(tokens["alice"]), "Idempotency-Key": "cfg-admin-1"},
        )
        assert saved.status_code == 200
        assert saved.json()["version"] == version + 1

    _run_scenario(app, steps)


def test_credential_rest_rejects_server_metadata_ssrf_and_provider_switch(
    tmp_path, monkeypatch,
):
    app = _load_app(tmp_path, monkeypatch)
    monkeypatch.setenv("ZE_SUPER_ADMIN_USERS", "alice")

    async def steps(client):
        registered = await client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "alice-pass-1"},
        )
        token = registered.json()["token"]
        client.cookies.clear()
        auth = _auth(token)

        # P5 之后公网 https 端点可登记(用户自绑自己的网关),所以这里列的是护栏真正
        # 该拦的那些:服务端自有字段、环回/私网/链路本地、非 https、userinfo、query。
        for index, metadata in enumerate((
            {"is_default": True},
            {"home_dir": "/tmp/escape"},
            {"bin": "/tmp/attacker"},
            {"base_url": "http://127.0.0.1/internal"},
            {"base_url": "https://192.168.1.5/v1"},
            {"base_url": "https://169.254.169.254/latest/meta"},
            {"base_url": "http://evil.example/v1"},
            {"base_url": "https://user:pw@evil.example/v1"},
            {"base_url": "https://evil.example/v1?x=1"},
        )):
            rejected = await client.post(
                "/api/account/credentials",
                json={"provider": "hupo_gateway_key", "metadata": metadata},
                headers={**auth, "Idempotency-Key": f"cred-reject-{index}"},
            )
            assert rejected.status_code == 422

        headers = {**auth, "Idempotency-Key": "cred-safe-create"}
        payload = {
            "provider": "hupo_gateway_key",
            "secret": "sk-never-echo",
            "metadata": {"base_url": "https://api.ai.hupo.games/v1"},
        }
        created = await client.post(
            "/api/account/credentials", json=payload, headers=headers,
        )
        replay = await client.post(
            "/api/account/credentials", json=payload, headers=headers,
        )
        assert created.status_code == replay.status_code == 200
        assert created.json() == replay.json()
        public = created.json()["credential"]
        encoded = str(public)
        assert "sk-never-echo" not in encoded
        assert "secret_ref" not in public
        assert "home_dir" not in public["metadata"]

        switched = await client.post(
            "/api/account/credentials",
            json={"id": public["id"], "provider": "codex_cli_profile"},
            headers={**auth, "Idempotency-Key": "cred-switch-1"},
        )
        assert switched.status_code == 409
        assert switched.json()["error"]["code"] == "credential_provider_immutable"

    _run_scenario(app, steps)
