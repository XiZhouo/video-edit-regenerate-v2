"""CredentialStore strict-load and single-file transaction evidence."""
from __future__ import annotations

import errno
import json
import multiprocessing
import os
import stat
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import credentials as credentials_mod  # noqa: E402
from credentials import (  # noqa: E402
    CREDENTIAL_STORE_SCHEMA_VERSION,
    CredentialStore,
    PROVIDER_CODEX_CLI,
    PROVIDER_HUPO_GATEWAY,
    write_cli_auth_file,
)
from entity import ZeError  # noqa: E402


def _store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(
        path=tmp_path / "credentials.json",
        key_path=tmp_path / "credentials.key",
    )


def _process_upsert(
    path: str,
    key_path: str,
    credential_id: str,
    start: object,
    result: object,
) -> None:
    try:
        store = CredentialStore(path=path, key_path=key_path)
        start.wait(timeout=10)  # type: ignore[attr-defined]
        store.upsert_for_user(
            "feishu:parallel",
            credential_id=credential_id,
            provider=PROVIDER_CODEX_CLI,
            label=credential_id,
        )
        result.put("")  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - child must report rather than hang
        result.put(repr(exc))  # type: ignore[attr-defined]


def test_current_1_0_0_file_is_exactly_compatible_across_restart(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_1",
        credential_id="cred_current",
        provider=PROVIDER_HUPO_GATEWAY,
        secret="sk-current",
        metadata={"base_url": "https://gateway.example/v1"},
    )
    raw = json.loads(store.path.read_text(encoding="utf-8"))

    assert raw["schema_version"] == CREDENTIAL_STORE_SCHEMA_VERSION == "1.0.0"
    assert set(raw) == {"schema_version", "credentials", "secrets"}
    assert set(raw["credentials"][0]) == set(credentials_mod.CredentialRecord.__dataclass_fields__)

    restarted = _store(tmp_path)
    assert restarted.get_for_user("feishu:ou_1", rec.id).metadata == {
        "base_url": "https://gateway.example/v1",
    }
    assert restarted.reveal_secret("feishu:ou_1", rec.id) == "sk-current"


@pytest.mark.parametrize(
    ("payload", "stage"),
    [
        ("{not-json", "decode"),
        ("[]", "shape"),
        (json.dumps({"schema_version": "1.0.0", "credentials": []}), "shape"),
        ('{"schema_version":"1.0.0","schema_version":"1.0.0",'
         '"credentials":[],"secrets":{}}', "decode"),
    ],
)
def test_malformed_existing_store_fails_closed_without_clearing_memory(
    tmp_path, payload, stage,
):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1", credential_id="cred_old", provider=PROVIDER_CODEX_CLI,
    )
    store.path.write_text(payload, encoding="utf-8")

    with pytest.raises(ZeError) as caught:
        store.list_for_user("feishu:ou_1")

    assert caught.value.code == "credential_store_corrupt"
    assert caught.value.context["stage"] == stage
    assert "cred_old" in store._records


@pytest.mark.parametrize(
    "mutation", ["bad_record", "missing_secret", "orphan_secret", "bad_mac", "wrong_last4"],
)
def test_bad_record_secret_and_cross_reference_are_never_skipped(tmp_path, mutation):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1",
        credential_id="cred_sealed",
        provider=PROVIDER_HUPO_GATEWAY,
        secret="sk-secret",
    )
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    if mutation == "bad_record":
        raw["credentials"][0]["provider"] = "invented_provider"
    elif mutation == "missing_secret":
        raw["secrets"] = {}
    elif mutation == "orphan_secret":
        raw["secrets"]["secret_cred_orphan"] = dict(next(iter(raw["secrets"].values())))
    elif mutation == "bad_mac":
        ref = raw["credentials"][0]["secret_ref"]
        raw["secrets"][ref]["mac"] = "AAAA"
    else:
        raw["credentials"][0]["last4"] = "nope"
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ZeError) as caught:
        store.mark_used("feishu:ou_1", "cred_sealed")

    assert caught.value.code == "credential_store_corrupt"
    assert caught.value.context["stage"] in {"record_validate", "secret_validate"}
    assert store._records["cred_sealed"].last_used_at == 0.0


def test_missing_or_malformed_key_for_existing_secret_fails_closed(tmp_path):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1",
        credential_id="cred_sealed",
        provider=PROVIDER_HUPO_GATEWAY,
        secret="sk-secret",
    )
    (tmp_path / "credentials.key").write_bytes(b"too-short")

    with pytest.raises(ZeError) as caught:
        CredentialStore(
            path=tmp_path / "credentials.json",
            key_path=tmp_path / "credentials.key",
        )

    assert caught.value.code == "credential_store_corrupt"
    assert caught.value.context["stage"] == "secret_validate"
    assert caught.value.context["cause_code"] == "credential_key_invalid"


def test_legacy_long_key_file_keeps_1_0_first_32_bytes_compatibility(tmp_path):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1",
        credential_id="cred_long_key",
        provider=PROVIDER_HUPO_GATEWAY,
        secret="sk-secret",
    )
    key_path = tmp_path / "credentials.key"
    key_path.write_bytes(key_path.read_bytes() + b"legacy-trailing-key-material")

    restarted = _store(tmp_path)
    assert restarted.reveal_secret("feishu:ou_1", "cred_long_key") == "sk-secret"


def test_existing_store_and_key_permissions_are_tightened_on_restart(tmp_path):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1", credential_id="cred_private",
        provider=PROVIDER_HUPO_GATEWAY, secret="sk-private",
    )
    store.path.chmod(0o644)
    (tmp_path / "credentials.key").chmod(0o644)

    restarted = _store(tmp_path)

    assert stat.S_IMODE(restarted.path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "credentials.key").stat().st_mode) == 0o600
    assert restarted.reveal_secret("feishu:ou_1", "cred_private") == "sk-private"


def test_existing_store_symlink_is_rejected(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"schema_version":"1.0.0","credentials":[],"secrets":{}}')
    path = tmp_path / "credentials.json"
    path.symlink_to(outside)

    with pytest.raises(ZeError) as caught:
        CredentialStore(path=path, key_path=tmp_path / "credentials.key")

    assert caught.value.code == "credential_store_permissions_invalid"


@pytest.mark.parametrize(
    ("target", "stage"),
    [
        ("tempfile.mkstemp", "temp_create"),
        ("_write_stream", "write"),
        ("_flush_stream", "flush"),
        ("_fsync_stream", "file_fsync"),
        ("os.replace", "replace"),
    ],
)
def test_every_pre_replace_failure_keeps_old_memory_and_complete_old_file(
    tmp_path, monkeypatch, target, stage,
):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1",
        credential_id="cred_old",
        provider=PROVIDER_CODEX_CLI,
        label="old",
    )
    old_bytes = store.path.read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError(errno.EIO, f"injected {stage}")

    if target == "tempfile.mkstemp":
        monkeypatch.setattr(credentials_mod.tempfile, "mkstemp", fail)
    elif target == "os.replace":
        monkeypatch.setattr(credentials_mod.os, "replace", fail)
    else:
        monkeypatch.setattr(credentials_mod, target, fail)

    with pytest.raises(ZeError) as caught:
        store.upsert_for_user(
            "feishu:ou_1",
            credential_id="cred_old",
            provider=PROVIDER_CODEX_CLI,
            label="new",
        )

    assert caught.value.code == "credential_store_write_failed"
    assert caught.value.context == {
        "stage": stage,
        "path": str(store.path),
        "replaced": False,
    }
    assert store.path.read_bytes() == old_bytes
    assert store._records["cred_old"].label == "old"
    assert list(tmp_path.glob(".credentials.json.*.tmp")) == []


@pytest.mark.parametrize(
    "mutation", ["delete", "mark_used", "mark_verified", "set_default"],
)
def test_every_other_mutator_uses_the_same_transaction_boundary(
    tmp_path, monkeypatch, mutation,
):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1",
        credential_id="cred_target",
        provider=PROVIDER_HUPO_GATEWAY,
        label="old",
        secret="sk-old",
    )
    old_bytes = store.path.read_bytes()
    old_record = store._copy_record(store._records["cred_target"])

    def fail(*_args, **_kwargs):
        raise OSError(errno.EIO, f"injected {mutation}")

    monkeypatch.setattr(credentials_mod, "_write_stream", fail)
    with pytest.raises(ZeError) as caught:
        if mutation == "delete":
            store.delete_for_user("feishu:ou_1", "cred_target")
        elif mutation == "mark_used":
            store.mark_used("feishu:ou_1", "cred_target")
        elif mutation == "set_default":
            store.set_default_for_user("feishu:ou_1", "cred_target")
        else:
            store.mark_verified("feishu:ou_1", "cred_target")

    assert caught.value.code == "credential_store_write_failed"
    assert store.path.read_bytes() == old_bytes
    assert store._records["cred_target"] == old_record


def test_set_default_updates_the_whole_user_aggregate_with_one_replace(
    tmp_path, monkeypatch,
):
    store = _store(tmp_path)
    for cid in ("cred_first", "cred_second", "cred_third"):
        store.upsert_for_user(
            "feishu:ou_1", credential_id=cid, provider=PROVIDER_CODEX_CLI,
        )
    store.upsert_for_user(
        "feishu:other", credential_id="cred_other", provider=PROVIDER_CODEX_CLI,
        metadata={credentials_mod.META_DEFAULT: True},
    )
    store.set_default_for_user("feishu:ou_1", "cred_first")

    calls = {"count": 0}
    real_replace = credentials_mod.os.replace

    def count_replace(*args, **kwargs):
        calls["count"] += 1
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(credentials_mod.os, "replace", count_replace)
    selected = store.set_default_for_user("feishu:ou_1", "cred_third")

    assert selected.id == "cred_third"
    assert calls["count"] == 1
    mine = store.list_for_user("feishu:ou_1")
    assert [row.id for row in mine if row.metadata.get(credentials_mod.META_DEFAULT)] == [
        "cred_third",
    ]
    assert store.get_for_user("feishu:other", "cred_other").metadata[
        credentials_mod.META_DEFAULT
    ] is True


def test_verify_without_required_secret_is_read_only(tmp_path):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1",
        credential_id="cred_no_secret",
        provider=PROVIDER_HUPO_GATEWAY,
    )
    old_bytes = store.path.read_bytes()
    old_record = store.get_for_user("feishu:ou_1", "cred_no_secret")

    with pytest.raises(ZeError) as caught:
        store.mark_verified("feishu:ou_1", "cred_no_secret")

    assert caught.value.code == "requires_credential"
    assert store.path.read_bytes() == old_bytes
    assert store.get_for_user("feishu:ou_1", "cred_no_secret") == old_record


def test_provider_is_immutable_and_never_carries_secret_across_kind(tmp_path):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1", credential_id="cred_fixed",
        provider=PROVIDER_HUPO_GATEWAY, secret="sk-stays-sealed",
    )
    old_bytes = store.path.read_bytes()

    with pytest.raises(ZeError) as caught:
        store.upsert_for_user(
            "feishu:ou_1", credential_id="cred_fixed",
            provider=PROVIDER_CODEX_CLI,
        )

    assert caught.value.code == "credential_provider_immutable"
    assert store.path.read_bytes() == old_bytes
    assert store.reveal_secret("feishu:ou_1", "cred_fixed") == "sk-stays-sealed"


def test_partial_edit_preserves_capabilities_and_none_clears_metadata(tmp_path):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1", credential_id="cred_edit",
        provider=PROVIDER_CODEX_CLI,
        capabilities=["llm", "custom-cap"],
        metadata={"account": "old", "profile": "keep"},
    )
    rec = store.upsert_for_user(
        "feishu:ou_1", credential_id="cred_edit",
        provider=PROVIDER_CODEX_CLI, label="renamed",
        metadata={"account": None},
    )
    assert rec.capabilities == ("llm", "custom-cap")
    assert rec.metadata == {"profile": "keep"}


def test_directory_fsync_failure_reports_unknown_and_memory_follows_new_file(
    tmp_path, monkeypatch,
):
    store = _store(tmp_path)
    store.upsert_for_user(
        "feishu:ou_1",
        credential_id="cred_old",
        provider=PROVIDER_CODEX_CLI,
        label="old",
    )

    def fail(_path: Path) -> bool:
        raise OSError(errno.EIO, "injected directory fsync")

    monkeypatch.setattr(credentials_mod, "_fsync_parent", fail)
    with pytest.raises(ZeError) as caught:
        store.upsert_for_user(
            "feishu:ou_1",
            credential_id="cred_old",
            provider=PROVIDER_CODEX_CLI,
            label="new",
        )

    assert caught.value.code == "credential_store_durability_unknown"
    assert caught.value.context == {
        "stage": "directory_fsync",
        "path": str(store.path),
        "replaced": True,
    }
    assert store._records["cred_old"].label == "new"
    assert json.loads(store.path.read_text(encoding="utf-8"))["credentials"][0]["label"] == "new"
    assert list(tmp_path.glob(".credentials.json.*.tmp")) == []

    restarted = _store(tmp_path)
    assert restarted.get_for_user("feishu:ou_1", "cred_old").label == "new"


def test_two_instances_reload_inside_lock_so_sequential_writers_do_not_lose_updates(tmp_path):
    first = _store(tmp_path)
    second = _store(tmp_path)

    first.upsert_for_user(
        "feishu:ou_1", credential_id="cred_first", provider=PROVIDER_CODEX_CLI,
    )
    second.upsert_for_user(
        "feishu:ou_1", credential_id="cred_second", provider=PROVIDER_CODEX_CLI,
    )

    assert {row.id for row in first.list_for_user("feishu:ou_1")} == {
        "cred_first", "cred_second",
    }

    # A stale second instance must also preserve the first writer's metadata
    # when its operation only touches verification timestamps.
    first.upsert_for_user(
        "feishu:ou_1",
        credential_id="cred_first",
        provider=PROVIDER_CODEX_CLI,
        metadata={"account": "preserve-me"},
    )
    second.mark_verified("feishu:ou_1", "cred_first")
    assert first.get_for_user("feishu:ou_1", "cred_first").metadata == {
        "account": "preserve-me",
    }


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="needs fork")
def test_two_processes_share_file_lock_without_lost_update(tmp_path):
    _store(tmp_path)
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    result = ctx.Queue()
    args = (str(tmp_path / "credentials.json"), str(tmp_path / "credentials.key"))
    processes = [
        ctx.Process(target=_process_upsert, args=(*args, credential_id, start, result))
        for credential_id in ("cred_process_a", "cred_process_b")
    ]
    for process in processes:
        process.start()
    start.set()
    errors = [result.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert errors == ["", ""]
    assert {row.id for row in _store(tmp_path).list_for_user("feishu:parallel")} == {
        "cred_process_a", "cred_process_b",
    }


def test_returned_records_are_detached_copies(tmp_path):
    store = _store(tmp_path)
    rec = store.upsert_for_user(
        "feishu:ou_1",
        credential_id="cred_copy",
        provider=PROVIDER_CODEX_CLI,
        metadata={"account": "real"},
    )
    rec.label = "tampered"
    rec.metadata["account"] = "tampered"
    listed = store.list_for_user("feishu:ou_1")
    listed[0].metadata["account"] = "also-tampered"

    persisted = store.get_for_user("feishu:ou_1", "cred_copy")
    assert persisted.label != "tampered"
    assert persisted.metadata == {"account": "real"}


def test_cli_auth_file_is_its_own_atomic_file_aggregate(tmp_path, monkeypatch):
    home = tmp_path / "cli-home"
    target = write_cli_auth_file(
        home, PROVIDER_CODEX_CLI, json.dumps({"auth_mode": "chatgpt", "token": "old"}),
    )
    old_bytes = target.read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError(errno.EIO, "injected replace")

    monkeypatch.setattr(credentials_mod.os, "replace", fail)
    with pytest.raises(ZeError) as caught:
        write_cli_auth_file(
            home, PROVIDER_CODEX_CLI, json.dumps({"auth_mode": "chatgpt", "token": "new"}),
        )

    assert caught.value.code == "credential_cli_auth_write_failed"
    assert caught.value.context["stage"] == "replace"
    assert target.read_bytes() == old_bytes
    assert list(home.glob(".auth.json.*.tmp")) == []


def test_cli_auth_directory_fsync_unknown_still_leaves_complete_new_file(
    tmp_path, monkeypatch,
):
    home = tmp_path / "cli-home"
    write_cli_auth_file(home, PROVIDER_CODEX_CLI, json.dumps({"token": "old"}))

    def fail(_path: Path) -> bool:
        raise OSError(errno.EIO, "injected directory fsync")

    monkeypatch.setattr(credentials_mod, "_fsync_parent", fail)
    with pytest.raises(ZeError) as caught:
        write_cli_auth_file(home, PROVIDER_CODEX_CLI, json.dumps({"token": "new"}))

    assert caught.value.code == "credential_cli_auth_durability_unknown"
    assert caught.value.context["replaced"] is True
    assert json.loads((home / "auth.json").read_text(encoding="utf-8"))["token"] == "new"


def test_non_finite_metadata_and_cli_json_are_rejected_before_write(tmp_path):
    store = _store(tmp_path)
    old_bytes = store.path.read_bytes()
    with pytest.raises(ZeError) as caught:
        store.upsert_for_user(
            "feishu:ou_1",
            provider=PROVIDER_CODEX_CLI,
            metadata={"bad": float("nan")},
        )
    assert caught.value.code == "validation_failed"
    assert store.path.read_bytes() == old_bytes

    with pytest.raises(ZeError) as caught:
        write_cli_auth_file(tmp_path / "cli-home", PROVIDER_CODEX_CLI, '{"bad": NaN}')
    assert caught.value.code == "validation_failed"
    assert not (tmp_path / "cli-home" / "auth.json").exists()


def test_key_prepare_failure_never_creates_a_dangling_record(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError(errno.EIO, "injected key replace")

    monkeypatch.setattr(credentials_mod.os, "replace", fail)
    with pytest.raises(ZeError) as caught:
        store.upsert_for_user(
            "feishu:ou_1",
            credential_id="cred_key_fail",
            provider=PROVIDER_HUPO_GATEWAY,
            secret="sk-never-persisted",
        )

    assert caught.value.code == "credential_key_write_failed"
    assert json.loads(store.path.read_text(encoding="utf-8"))["credentials"] == []
    assert store._records == {}
    assert not (tmp_path / "credentials.key").exists()
    assert list(tmp_path.glob(".credentials.key.*.tmp")) == []


def test_legacy_capability_snapshot_loads_and_migrates(tmp_path):
    """3.56 回归(部署实证):存量 codex 绑定(capabilities=["llm","agent"] 历史默认快照)
    落盘后重开 store 不判 corrupt,读出自动含 image;显式定制集合仍严格 canonical。"""
    store = _store(tmp_path)
    store.upsert_for_user("feishu:ou_1", credential_id="cred_old",
                          provider=PROVIDER_CODEX_CLI,
                          capabilities=["llm", "agent"])
    # 手工把落盘值改回历史快照形态(upsert 已存升级后形态,模拟 3.56 前写入的旧文件)
    import json as _json
    doc = _json.loads(store.path.read_text(encoding="utf-8"))
    for rec in doc["credentials"]:
        if rec["id"] == "cred_old":
            rec["capabilities"] = ["llm", "agent"]
    store.path.write_text(_json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    store2 = _store(tmp_path)
    rows = store2.list_for_user("feishu:ou_1")
    rec = next(r for r in rows if r.id == "cred_old")
    assert "image" in rec.capabilities
