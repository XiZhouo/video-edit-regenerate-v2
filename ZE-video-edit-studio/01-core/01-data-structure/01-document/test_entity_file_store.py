from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

import entity_file_store as store_module
from entity_file_store import (
    ENTITY_STORE_DIRECTORY,
    EntityFileStore,
    EntityFileStoreError,
    entity_identity_digest,
)


def test_identity_digest_is_framed_and_path_independent() -> None:
    assert entity_identity_digest("card", "row-1") == (
        "589951de5dc89a140611dcadefbf1676f551dce49a051804f0f067a9095fa6d4"
    )
    assert entity_identity_digest("a", "bc") != entity_identity_digest("ab", "c")
    for entity_type, entity_id in (
        ("../type", "/absolute/id"),
        ("类型/一", "..\\二"),
        ("x" * 4096, "y" * 4096),
    ):
        digest = entity_identity_digest(entity_type, entity_id)
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")


def test_store_creates_only_one_missing_parent_and_round_trips_generation(
    tmp_path: Path,
) -> None:
    document_path = tmp_path / "project" / "document.json"
    digest = entity_identity_digest("a/b", "../entity")
    with EntityFileStore(document_path, create_parent=True) as store:
        store.acquire_exclusive_lock()
        store.audit_before_save()
        store.write_generation(2, {digest: b'{"type":"a/b","id":"../entity"}'})
        store.commit_document(b'{"_v":2}')
        assert store.read_v2_entity(2, "a/b", "../entity").startswith(b"{")

    assert document_path.read_bytes() == b'{"_v":2}'
    assert not (tmp_path / "project" / "entities" / "a").exists()


def test_entity_reads_apply_private_byte_limit_before_allocating_payload(
    tmp_path: Path,
) -> None:
    document_path = tmp_path / "project" / "document.json"
    payload = b'{"type":"card","id":"row","data":{"value":1}}'
    digest = entity_identity_digest("card", "row")
    with EntityFileStore(document_path, create_parent=True) as store:
        store.acquire_exclusive_lock()
        store.write_generation(1, {digest: payload})
        with pytest.raises(EntityFileStoreError) as captured:
            store.read_v2_entity(1, "card", "row", _maximum_bytes=len(payload) - 1)
        assert captured.value.code == "document_storage_layout_invalid"
        assert captured.value.reason == "file_size_invalid"

        for invalid in (True, -1, 1.0, "1"):
            with pytest.raises(TypeError, match="exact non-negative"):
                store.read_v2_entity(1, "card", "row", _maximum_bytes=invalid)
            with pytest.raises(TypeError, match="exact non-negative"):
                store.read_legacy_entity("card", "row", _maximum_bytes=invalid)


def test_store_safely_creates_recursive_parent_chain(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "nested" / "document.json"
    with EntityFileStore(path, create_parent=True) as store:
        store.acquire_exclusive_lock()
        store.commit_document(b'{"_v":1}')
    assert path.read_bytes() == b'{"_v":1}'
    assert os.stat(path.parent).st_mode & 0o077 == 0


def test_store_rejects_existing_intermediate_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    (target / "deep").mkdir(parents=True)
    intermediate = tmp_path / "middle"
    intermediate.symlink_to(target, target_is_directory=True)
    path = intermediate / "deep" / "document.json"

    with pytest.raises(EntityFileStoreError) as captured:
        EntityFileStore(path, create_parent=True)
    assert captured.value.code == "document_storage_path_unsafe"
    assert not (target / "deep" / "document.json").exists()


def test_document_commit_reports_visible_but_unknown_after_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    with EntityFileStore(path, create_parent=True) as store:
        store.acquire_exclusive_lock()
        real_fsync = os.fsync
        calls = 0

        def fail_root_fsync(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.EIO, "simulated directory fsync failure")
            real_fsync(descriptor)

        monkeypatch.setattr(store_module.os, "fsync", fail_root_fsync)
        with pytest.raises(EntityFileStoreError) as captured:
            store.commit_document(b'{"_v":2}')

    assert captured.value.code == "document_durability_unknown"
    assert captured.value.committed is True
    assert path.read_bytes() == b'{"_v":2}'


def test_legacy_hardlink_is_rejected_without_reading_alias(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_bytes(b'{"secret":true}')
    sentinel.chmod(0o600)
    type_dir = tmp_path / "entities" / "safe"
    type_dir.mkdir(parents=True)
    os.link(sentinel, type_dir / "row.json")

    with EntityFileStore(tmp_path / "document.json", create_parent=True) as store:
        store.acquire_shared_lock()
        with pytest.raises(EntityFileStoreError) as captured:
            store.read_legacy_entity("safe", "row")

    assert captured.value.code == "document_storage_path_unsafe"
    assert sentinel.read_bytes() == b'{"secret":true}'


def test_audit_never_follows_stale_temp_symlink_during_cleanup(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_bytes(b'{"outside":true}')
    sentinel.chmod(0o600)
    shard = (
        tmp_path
        / "entities"
        / ENTITY_STORE_DIRECTORY
        / "2"
        / "aa"
    )
    shard.mkdir(parents=True)
    for managed in (shard.parent.parent, shard.parent, shard):
        managed.chmod(0o700)
    hostile = shard / (".ze-tmp-" + "0" * 32 + ".tmp")
    hostile.symlink_to(sentinel)

    with EntityFileStore(tmp_path / "document.json", create_parent=True) as store:
        store.acquire_exclusive_lock()
        with pytest.raises(EntityFileStoreError) as captured:
            store.audit_before_save()

    assert captured.value.code == "document_storage_path_unsafe"
    assert hostile.is_symlink()
    assert sentinel.read_bytes() == b'{"outside":true}'


def test_file_write_and_file_fsync_errors_are_typed_pre_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    with EntityFileStore(path, create_parent=True) as store:
        store.acquire_exclusive_lock()
        monkeypatch.setattr(
            store_module.os,
            "write",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(errno.EIO, "write")),
        )
        with pytest.raises(EntityFileStoreError) as write_error:
            store.commit_document(b'{"_v":2}')
    assert write_error.value.code == "document_write_failed"
    assert write_error.value.committed is False
    assert not path.exists()

    monkeypatch.undo()
    with EntityFileStore(path, create_parent=True) as store:
        store.acquire_exclusive_lock()
        monkeypatch.setattr(
            store_module.os,
            "fsync",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(errno.EIO, "fsync")),
        )
        with pytest.raises(EntityFileStoreError) as fsync_error:
            store.commit_document(b'{"_v":2}')
    assert fsync_error.value.code == "document_write_failed"
    assert fsync_error.value.committed is False
    assert not path.exists()


def test_file_read_error_is_typed_and_does_not_include_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    path.write_bytes(b'{"_v":1}')
    with EntityFileStore(path, create_parent=False) as store:
        store.acquire_shared_lock()
        monkeypatch.setattr(
            store_module.os,
            "read",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(errno.EIO, "read")),
        )
        with pytest.raises(EntityFileStoreError) as captured:
            store.read_document()
    assert captured.value.code == "project_recovery_required"
    assert str(tmp_path) not in str(captured.value)


def test_stale_temp_unlink_error_is_typed_and_confined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shard = tmp_path / "entities" / ENTITY_STORE_DIRECTORY / "2" / "aa"
    shard.mkdir(parents=True)
    for managed in (shard.parent.parent, shard.parent, shard):
        managed.chmod(0o700)
    temp = shard / (".ze-tmp-" + "1" * 32 + ".tmp")
    temp.write_bytes(b"stale")
    temp.chmod(0o600)
    real_unlink = store_module.os.unlink

    def refuse_temp(name, *args, **kwargs):
        if name == temp.name:
            raise OSError(errno.EACCES, "refused")
        return real_unlink(name, *args, **kwargs)

    with EntityFileStore(tmp_path / "document.json", create_parent=True) as store:
        store.acquire_exclusive_lock()
        monkeypatch.setattr(store_module.os, "unlink", refuse_temp)
        with pytest.raises(EntityFileStoreError) as captured:
            store.audit_before_save()
    assert captured.value.code == "document_write_failed"
    assert temp.read_bytes() == b"stale"


def test_store_methods_require_declared_lock_mode(tmp_path: Path) -> None:
    path = tmp_path / "document.json"
    with EntityFileStore(path, create_parent=True) as unlocked:
        with pytest.raises(EntityFileStoreError) as read_error:
            unlocked.read_document_if_exists()
        with pytest.raises(EntityFileStoreError) as write_error:
            unlocked.commit_document(b'{"_v":1}')
    assert read_error.value.code == "document_lock_unavailable"
    assert read_error.value.reason == "shared_lock_required"
    assert write_error.value.reason == "shared_lock_required"

    with EntityFileStore(path, create_parent=True) as reader:
        reader.acquire_shared_lock()
        assert reader.read_document_if_exists() is None
        with pytest.raises(EntityFileStoreError) as captured:
            reader.write_generation(1, {})
    assert captured.value.code == "document_lock_unavailable"
    assert captured.value.reason == "exclusive_lock_required"


def test_exclusive_lock_wait_is_bounded_and_path_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    monkeypatch.setattr(store_module, "_LOCK_TIMEOUT_SECONDS", 0.03)
    with EntityFileStore(path, create_parent=True) as writer:
        writer.acquire_exclusive_lock()
        with EntityFileStore(path, create_parent=False) as blocked:
            with pytest.raises(EntityFileStoreError) as captured:
                blocked.acquire_shared_lock()
    assert captured.value.code == "document_lock_unavailable"
    assert captured.value.reason == "lock_wait_timeout"
    assert str(tmp_path) not in str(captured.value)


def test_lock_file_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"outside")
    sentinel.chmod(0o600)
    (tmp_path / ".ze-document.lock").symlink_to(sentinel)

    with EntityFileStore(tmp_path / "document.json", create_parent=True) as store:
        with pytest.raises(EntityFileStoreError) as captured:
            store.acquire_exclusive_lock()

    assert captured.value.code == "document_storage_path_unsafe"
    assert sentinel.read_bytes() == b"outside"


def test_non_sticky_writable_user_ancestor_is_rejected(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    with pytest.raises(EntityFileStoreError) as captured:
        EntityFileStore(unsafe / "document.json", create_parent=True)
    assert captured.value.code == "document_storage_path_unsafe"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux sticky /tmp semantics")
def test_linux_root_owned_sticky_tmp_is_a_safe_trust_anchor() -> None:
    anchor = Path("/tmp")
    info = anchor.stat()
    if info.st_uid != 0 or not (stat.S_IMODE(info.st_mode) & stat.S_ISVTX):
        pytest.skip("/tmp is not a root-owned sticky directory")
    project = Path(tempfile.mkdtemp(prefix="ze-document-lock-", dir=anchor))
    try:
        project.chmod(0o700)
        path = project / "nested" / "document.json"
        with EntityFileStore(path, create_parent=True) as store:
            store.acquire_exclusive_lock()
            store.commit_document(b'{"_v":1}')
        assert path.read_bytes() == b'{"_v":1}'
    finally:
        shutil.rmtree(project)
