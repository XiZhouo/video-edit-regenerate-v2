"""W2d sealed-v2 (AES-GCM) at-rest encryption evidence.

Proves the new authenticated-encryption envelope and its graceful degradation:
  1. sealed-v2 encrypt→decrypt round-trip (real AEAD backend)
  2. AAD binds ciphertext to its record (moved blob fails to open)
  3. lazy, zero-loss v1→v2 migration on upsert
  4. graceful degradation: no AEAD backend still seals/opens v1, never crashes
  5. reading a v2 blob without the backend fails closed (never plaintext)
  6. every prior invariant still holds under v2 (0600 / cross-user ACL /
     public_dict hides secret / tamper detection)

Tests that need to *produce* a real v2 blob skip when `cryptography` is absent;
the degradation tests force `_HAS_AEAD=False` and therefore run either way.
"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import credentials as credentials_mod  # noqa: E402
from credentials import (  # noqa: E402
    CREDENTIAL_SECRET_SCHEMA_V2,
    CREDENTIAL_SECRET_SCHEMA_VERSION,
    CredentialStore,
    PROVIDER_HUPO_GATEWAY,
    _open_sealed,
    _seal,
)
from entity import ZeError  # noqa: E402

needs_aead = pytest.mark.skipif(
    not credentials_mod._HAS_AEAD,
    reason="cryptography AEAD backend not installed",
)


def _store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(
        path=tmp_path / "credentials.json",
        key_path=tmp_path / "credentials.key",
    )


def _only_secret_blob(store: CredentialStore) -> dict:
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    return next(iter(raw["secrets"].values()))


# ---- 1. sealed-v2 round-trip ------------------------------------------------

@needs_aead
def test_new_secret_is_sealed_v2_and_round_trips(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_1", provider=PROVIDER_HUPO_GATEWAY, secret="sk-round-trip-9",
    )
    blob = _only_secret_blob(store)
    assert blob["schema"] == CREDENTIAL_SECRET_SCHEMA_V2 == "sealed-v2"
    assert blob["alg"] == "AESGCM" and blob["aad_ver"] == 1
    assert set(blob) == {"schema", "alg", "key_id", "nonce", "ciphertext", "aad_ver"}
    assert "sk-round-trip-9" not in store.path.read_text(encoding="utf-8")
    assert store.reveal_secret("feishu:ou_1", rec.id) == "sk-round-trip-9"


@needs_aead
def test_seal_v2_is_nondeterministic_random_nonce(tmp_path):
    key_path = tmp_path / "credentials.key"
    a = _seal("same-secret", key_path=key_path,
              owner="feishu:o", credential_id="cred_a", provider="hupo_gateway_key")
    b = _seal("same-secret", key_path=key_path,
              owner="feishu:o", credential_id="cred_a", provider="hupo_gateway_key")
    assert a["schema"] == "sealed-v2"
    assert a["nonce"] != b["nonce"] and a["ciphertext"] != b["ciphertext"]
    # both still open to the same plaintext
    for blob in (a, b):
        assert _open_sealed(
            blob, key_path=key_path,
            owner="feishu:o", credential_id="cred_a", provider="hupo_gateway_key",
        ) == "same-secret"


# ---- 2. AAD binds the ciphertext to its record ------------------------------

@needs_aead
def test_aad_binds_ciphertext_to_owner_id_provider(tmp_path):
    key_path = tmp_path / "credentials.key"
    blob = _seal("sk-bound", key_path=key_path,
                 owner="feishu:alice", credential_id="cred_a", provider="openai_api_key")

    # correct identity opens
    assert _open_sealed(
        blob, key_path=key_path,
        owner="feishu:alice", credential_id="cred_a", provider="openai_api_key",
    ) == "sk-bound"

    # a blob "moved" onto another record (different owner / id / provider) fails
    for wrong in (
        {"owner": "feishu:bob", "credential_id": "cred_a", "provider": "openai_api_key"},
        {"owner": "feishu:alice", "credential_id": "cred_b", "provider": "openai_api_key"},
        {"owner": "feishu:alice", "credential_id": "cred_a", "provider": "anthropic_api_key"},
    ):
        with pytest.raises(ZeError) as e:
            _open_sealed(blob, key_path=key_path, **wrong)
        assert e.value.code == "credential_unavailable"


# ---- 3. lazy, zero-loss v1 -> v2 migration ----------------------------------

@needs_aead
def test_upsert_lazily_migrates_v1_secret_to_v2_preserving_last4(tmp_path, monkeypatch):
    # Seal as v1 by pretending the backend is unavailable.
    monkeypatch.setattr(credentials_mod, "_HAS_AEAD", False)
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_1", credential_id="cred_legacy",
        provider=PROVIDER_HUPO_GATEWAY, secret="sk-legacy-value",
    )
    assert _only_secret_blob(store)["schema"] == CREDENTIAL_SECRET_SCHEMA_VERSION == "sealed-v1"
    assert rec.last4 == "alue"

    # Backend now present; a plain metadata upsert (no new secret) re-seals to v2.
    monkeypatch.setattr(credentials_mod, "_HAS_AEAD", True)
    migrated = store.upsert_for_user(
        "feishu:ou_1", credential_id="cred_legacy",
        provider=PROVIDER_HUPO_GATEWAY, label="renamed",
    )
    assert _only_secret_blob(store)["schema"] == "sealed-v2"
    assert migrated.last4 == "alue"  # last4 preserved across migration
    assert store.reveal_secret("feishu:ou_1", "cred_legacy") == "sk-legacy-value"

    # survives a restart (validated + opened as v2 on load)
    assert _store(tmp_path).reveal_secret("feishu:ou_1", "cred_legacy") == "sk-legacy-value"


@needs_aead
def test_migration_keeps_v1_when_secret_cannot_be_opened(tmp_path, monkeypatch):
    # Seal v1 under one key, then corrupt the key so v1 cannot be opened; the
    # migration must leave the v1 blob intact rather than drop the secret.
    monkeypatch.setattr(credentials_mod, "_HAS_AEAD", False)
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1", credential_id="cred_keep",
        provider=PROVIDER_HUPO_GATEWAY, secret="sk-keep-me",
    )
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    v1_blob = dict(next(iter(raw["secrets"].values())))
    assert v1_blob["schema"] == "sealed-v1"

    monkeypatch.setattr(credentials_mod, "_HAS_AEAD", True)
    # Directly exercise the migration helper with an unopenable v1 blob (bad mac).
    broken = dict(v1_blob)
    broken["mac"] = "AAAA"
    secrets_map = {"secret_cred_keep": broken}
    rec = store.get_for_user("feishu:ou_1", "cred_keep")
    credentials_mod._maybe_migrate_secret_v2(
        secrets_map, rec, key_path=store._key_path,
    )
    assert secrets_map["secret_cred_keep"] == broken  # untouched, v1 kept, zero-loss


# ---- 4. graceful degradation: no backend still works, never crashes ---------

def test_without_backend_seals_v1_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials_mod, "_HAS_AEAD", False)
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_1", provider=PROVIDER_HUPO_GATEWAY, secret="sk-degraded",
    )
    assert _only_secret_blob(store)["schema"] == "sealed-v1"
    assert store.reveal_secret("feishu:ou_1", rec.id) == "sk-degraded"
    # a fresh store instance reloads and opens the v1 blob without crypto
    assert _store(tmp_path).reveal_secret("feishu:ou_1", rec.id) == "sk-degraded"


def test_seal_helper_degrades_to_v1_without_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials_mod, "_HAS_AEAD", False)
    blob = _seal("x-secret", key_path=tmp_path / "credentials.key",
                 owner="feishu:o", credential_id="cred_a", provider="hupo_gateway_key")
    assert blob["schema"] == "sealed-v1"
    assert set(blob) == {"schema", "nonce", "ciphertext", "mac"}


# ---- 5. reading a v2 blob without backend fails closed (never plaintext) -----

@needs_aead
def test_open_v2_without_backend_fails_closed_not_plaintext(tmp_path, monkeypatch):
    key_path = tmp_path / "credentials.key"
    blob = _seal("sk-secret-v2", key_path=key_path,
                 owner="feishu:o", credential_id="cred_a", provider="hupo_gateway_key")
    assert blob["schema"] == "sealed-v2"

    monkeypatch.setattr(credentials_mod, "_HAS_AEAD", False)
    with pytest.raises(ZeError) as e:
        _open_sealed(blob, key_path=key_path,
                     owner="feishu:o", credential_id="cred_a", provider="hupo_gateway_key")
    assert e.value.code == "credential_crypto_unavailable"


@needs_aead
def test_store_with_v2_secret_fails_closed_when_backend_missing(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1", credential_id="cred_v2",
        provider=PROVIDER_HUPO_GATEWAY, secret="sk-only-v2",
    )
    assert _only_secret_blob(store)["schema"] == "sealed-v2"

    # A runtime that lost the backend must refuse to load the v2 store rather
    # than skip verification or expose anything.
    monkeypatch.setattr(credentials_mod, "_HAS_AEAD", False)
    with pytest.raises(ZeError) as e:
        _store(tmp_path)
    assert e.value.code == "credential_store_corrupt"
    assert e.value.context["stage"] == "secret_validate"
    assert e.value.context["cause_code"] == "credential_crypto_unavailable"


# ---- 6. prior invariants still hold under v2 --------------------------------

@needs_aead
def test_v2_store_file_is_0600_and_public_dict_hides_secret(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_1", provider=PROVIDER_HUPO_GATEWAY,
        secret="sk-private-v2", metadata={"base_url": "https://gw/v1"},
    )
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    public = rec.public_dict()
    assert public["has_secret"] is True and public["last4"] == "e-v2"
    assert "owner_user_id" not in public and "secret_ref" not in public
    assert "sk-private-v2" not in json.dumps(public)


@needs_aead
def test_v2_cross_user_acl_is_denied(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_1", credential_id="cred_mine",
        provider=PROVIDER_HUPO_GATEWAY, secret="sk-mine",
    )
    assert store.list_for_user("feishu:ou_2") == []
    with pytest.raises(ZeError) as e1:
        store.get_for_user("feishu:ou_2", rec.id)
    assert e1.value.code == "not_found"
    with pytest.raises(ZeError) as e2:
        store.upsert_for_user(
            "feishu:ou_2", credential_id="cred_mine", provider=PROVIDER_HUPO_GATEWAY,
        )
    assert e2.value.code == "acl_denied"
    with pytest.raises(ZeError) as e3:
        store.reveal_secret("feishu:ou_2", rec.id)
    assert e3.value.code == "not_found"


@needs_aead
@pytest.mark.parametrize("mutation", ["ciphertext", "nonce", "last4"])
def test_v2_tamper_is_detected_on_reload(tmp_path, mutation):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1", credential_id="cred_tamper",
        provider=PROVIDER_HUPO_GATEWAY, secret="sk-tamper-me",
    )
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    if mutation == "last4":
        raw["credentials"][0]["last4"] = "nope"
    else:
        ref = raw["credentials"][0]["secret_ref"]
        original = raw["secrets"][ref][mutation]
        # flip the first base64 char to a different valid one
        flipped = "B" if original[0] != "B" else "C"
        raw["secrets"][ref][mutation] = flipped + original[1:]
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ZeError) as e:
        store.reveal_secret("feishu:ou_1", "cred_tamper")
    assert e.value.code == "credential_store_corrupt"
    assert e.value.context["stage"] == "secret_validate"
