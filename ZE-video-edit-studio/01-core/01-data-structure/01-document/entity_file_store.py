"""Safe POSIX file boundary for legacy Document entity persistence.

Business identities are never interpreted as path components in the v2 layout.
The caller supplies already-serialized JSON bytes; this module owns only the
filesystem mapping, durable per-file replacement, safe legacy reads and
post-commit garbage collection.

Threat model: the project directory is owned by one cooperative Unix account.
Holding directory descriptors and using ``O_NOFOLLOW`` closes final-component
symlink races, but this is not a sandbox against a malicious process with the
same uid replacing higher ancestors concurrently.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Dict, Iterable, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - the public boundary reports POSIX support
    fcntl = None  # type: ignore[assignment]


ENTITY_STORAGE_SCHEMA = "ze.entity-files.v2"
ENTITY_STORAGE_LAYOUT = "document-version/shard/sha256-v1"
ENTITY_STORE_DIRECTORY = ".ze-store-v2"
DOCUMENT_LOCK_FILE = ".ze-document.lock"

# A process must never wait forever for a crashed or wedged peer.  Thirty seconds
# leaves room for a large fsync-heavy generation while remaining bounded for
# request/worker shutdown.  Tests may shorten the private constant.
_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.01

MAX_DOCUMENT_FILE_BYTES = 64 * 1024 * 1024
MAX_ENTITY_FILE_BYTES = 256 * 1024 * 1024
MAX_TREE_ENTRIES = 2_000_000
MAX_DOCUMENT_VERSION = 2**63 - 1

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SHARD = re.compile(r"^[0-9a-f]{2}$")
_TEMP = re.compile(r"^\.ze-tmp-[0-9a-f]{32}\.tmp$")
_UNSUPPORTED_DIR_FSYNC = frozenset(filter(None, (
    errno.EINVAL,
    errno.EBADF,
    getattr(errno, "ENOTSUP", None),
    getattr(errno, "EOPNOTSUPP", None),
)))


class EntityFileStoreError(RuntimeError):
    """Stable storage failure without absolute paths or business identities."""

    def __init__(self, code: str, reason: str, *, committed: bool = False) -> None:
        self.code = code
        self.reason = reason
        self.committed = committed
        super().__init__(code)


def entity_storage_marker() -> dict[str, str]:
    return {
        "schema": ENTITY_STORAGE_SCHEMA,
        "layout": ENTITY_STORAGE_LAYOUT,
    }


def is_entity_storage_marker(value: object) -> bool:
    return type(value) is dict and value == entity_storage_marker()


def require_legacy_component(value: object) -> str:
    """Return one strict UTF-8 POSIX component for a legacy raw identity.

    Safety comes from rejecting separators/NUL and resolving the accepted
    component relative to held directory descriptors with ``O_NOFOLLOW``.  It
    does not require an ASCII business-identity subset: historical projects
    legitimately contain Unicode, spaces and long filesystem-valid names.
    """
    if type(value) is not str or not value or value in (".", ".."):
        raise EntityFileStoreError(
            "document_storage_layout_invalid", "legacy_component_invalid"
        )
    if "/" in value or "\x00" in value:
        raise EntityFileStoreError(
            "document_storage_layout_invalid", "legacy_component_invalid"
        )
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise EntityFileStoreError(
            "document_storage_layout_invalid", "legacy_component_utf8_invalid"
        ) from None
    return value


def require_legacy_type_component(value: object) -> str:
    """Return one legacy entity-type component outside the v2 namespace."""
    component = require_legacy_component(value)
    if component == ENTITY_STORE_DIRECTORY:
        raise EntityFileStoreError(
            "document_storage_layout_invalid", "legacy_type_reserved"
        )
    return component


def entity_identity_digest(entity_type: object, entity_id: object) -> str:
    if type(entity_type) is not str or not entity_type:
        raise EntityFileStoreError(
            "document_storage_layout_invalid", "entity_type_invalid"
        )
    if type(entity_id) is not str or not entity_id:
        raise EntityFileStoreError(
            "document_storage_layout_invalid", "entity_id_invalid"
        )
    try:
        type_bytes = entity_type.encode("utf-8", "strict")
        id_bytes = entity_id.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise EntityFileStoreError(
            "document_storage_layout_invalid", "entity_identity_utf8_invalid"
        ) from None
    if len(type_bytes) >= 2**64 or len(id_bytes) >= 2**64:
        raise EntityFileStoreError(
            "document_storage_layout_invalid", "entity_identity_too_large"
        )
    hasher = hashlib.sha256()
    hasher.update(b"ze.entity-file.v2\0")
    hasher.update(len(type_bytes).to_bytes(8, "big"))
    hasher.update(type_bytes)
    hasher.update(len(id_bytes).to_bytes(8, "big"))
    hasher.update(id_bytes)
    return hasher.hexdigest()


def _unsafe(reason: str, *, committed: bool = False) -> EntityFileStoreError:
    return EntityFileStoreError(
        "document_storage_path_unsafe", reason, committed=committed
    )


def _unsupported(reason: str) -> EntityFileStoreError:
    return EntityFileStoreError("document_storage_unsupported", reason)


def _require_platform() -> None:
    if os.name != "posix":
        raise _unsupported("posix_required")
    if fcntl is None or not hasattr(fcntl, "flock"):
        raise _unsupported("flock_required")
    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise _unsupported("nofollow_directory_flags_required")
    required_dir_fd = (os.open, os.mkdir, os.rename, os.stat, os.unlink, os.rmdir)
    if any(function not in os.supports_dir_fd for function in required_dir_fd):
        raise _unsupported("dir_fd_required")
    if os.stat not in os.supports_follow_symlinks or os.listdir not in os.supports_fd:
        raise _unsupported("fd_stat_list_required")


def _owner_matches(info: os.stat_result) -> bool:
    return not hasattr(os, "geteuid") or info.st_uid == os.geteuid()


def _validate_directory(info: os.stat_result, *, private: bool = False) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise _unsafe("entry_not_directory")
    if not _owner_matches(info):
        raise _unsafe("entry_wrong_owner")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022:
        raise _unsafe("directory_group_or_world_writable")
    if private and mode & 0o077:
        raise _unsafe("managed_directory_not_private")


def _is_root_trust_anchor(info: os.stat_result) -> bool:
    """Whether a root-owned ancestor safely terminates the user path walk.

    Normal non-writable root directories are trusted.  Linux/macOS shared temp
    directories such as ``/tmp`` are also usable only when the sticky bit is
    present: other users may create siblings but cannot replace this user's
    owned project child.  A writable, non-sticky root directory is not safe.
    """
    if info.st_uid != 0 or not stat.S_ISDIR(info.st_mode):
        return False
    mode = stat.S_IMODE(info.st_mode)
    return not (mode & 0o022) or bool(mode & stat.S_ISVTX)


def _validate_file(info: os.stat_result, *, private: bool = False) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise _unsafe("entry_not_regular_file")
    if info.st_nlink != 1:
        raise _unsafe("entry_not_singly_linked")
    if not _owner_matches(info):
        raise _unsafe("entry_wrong_owner")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022:
        raise _unsafe("file_group_or_world_writable")
    if private and mode & 0o077:
        raise _unsafe("managed_file_not_private")


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _close_descriptor(descriptor: int) -> None:
    """Best-effort close for read/directory descriptors at an error boundary."""
    try:
        os.close(descriptor)
    except OSError:
        # The descriptor is no longer used.  Surfacing close errors from a read
        # or directory handle would mask the stable storage diagnosis already
        # being returned and cannot improve durability proof.
        pass


def _fstat(descriptor: int) -> os.stat_result:
    try:
        return os.fstat(descriptor)
    except OSError:
        raise _unsafe("descriptor_stat_failed") from None


def _list_directory(descriptor: int) -> list[str]:
    try:
        return os.listdir(descriptor)
    except OSError:
        raise _unsafe("directory_list_failed") from None


def _fsync_file(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError:
        raise EntityFileStoreError(
            "document_write_failed", "file_fsync_failed"
        ) from None


def _validate_user_ancestor_chain(path: Path) -> None:
    """Reject a user-controlled intermediate symlink before the trust anchor.

    Absolute ``open`` only protects its final component.  Walk lexical parents
    upward as final components too, stopping at the first non-writable root-
    owned system directory.  This keeps normal macOS/Linux system ancestors
    usable while proving every user-owned component down to ``path`` is a real,
    owner-safe directory rather than a followed symlink.
    """
    cursor = path
    while True:
        try:
            descriptor = os.open(os.fspath(cursor), _directory_flags())
        except OSError:
            raise _unsafe("project_ancestor_unsafe") from None
        try:
            info = _fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode):
                raise _unsafe("project_ancestor_not_directory")
            owner = info.st_uid
            mode = stat.S_IMODE(info.st_mode)
        finally:
            _close_descriptor(descriptor)
        current_owner = not hasattr(os, "geteuid") or owner == os.geteuid()
        if current_owner:
            if mode & 0o022:
                if owner == 0 and mode & stat.S_ISVTX:
                    return
                raise _unsafe("project_ancestor_group_or_world_writable")
            parent = cursor.parent
            if parent == cursor:
                return
            cursor = parent
            continue
        if _is_root_trust_anchor(info):
            return
        if owner == 0:
            raise _unsafe("project_ancestor_group_or_world_writable")
        raise _unsafe("project_ancestor_wrong_owner")


def _open_existing_ancestor(path: Path) -> int:
    """Open a missing-path anchor, including a root-owned sticky temp dir."""
    try:
        descriptor = os.open(os.fspath(path), _directory_flags())
    except FileNotFoundError:
        raise
    except OSError:
        raise _unsafe("project_directory_unavailable") from None
    try:
        info = _fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise _unsafe("entry_not_directory")
        if _is_root_trust_anchor(info):
            return descriptor
        _validate_directory(info)
        return descriptor
    except BaseException:
        _close_descriptor(descriptor)
        raise


def _open_directory_at(
    parent_fd: int,
    name: str,
    *,
    create: bool = False,
    private: bool = False,
) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            _fsync_directory(parent_fd, before_commit=True)
        except FileExistsError:
            pass
        except OSError:
            raise _unsafe("managed_directory_create_failed") from None
        try:
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
        except OSError:
            raise _unsafe("managed_directory_open_failed") from None
    except OSError:
        raise _unsafe("managed_directory_open_failed") from None
    try:
        _validate_directory(_fstat(descriptor), private=private)
    except BaseException:
        _close_descriptor(descriptor)
        raise
    return descriptor


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise _unsafe("entry_stat_failed") from None


def _fsync_directory(descriptor: int, *, before_commit: bool) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIR_FSYNC:
            code = (
                "document_storage_unsupported"
                if before_commit
                else "document_durability_unknown"
            )
            raise EntityFileStoreError(
                code,
                "directory_fsync_unsupported",
                committed=not before_commit,
            ) from None
        code = (
            "document_write_failed"
            if before_commit
            else "document_durability_unknown"
        )
        raise EntityFileStoreError(
            code,
            "directory_fsync_failed",
            committed=not before_commit,
        ) from None


def _open_lock_file(root_fd: int) -> int:
    """Open/create the stable project lock inode without following aliases."""
    flags = (
        os.O_RDWR
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    created = False
    try:
        descriptor = os.open(
            DOCUMENT_LOCK_FILE,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=root_fd,
        )
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(DOCUMENT_LOCK_FILE, flags, dir_fd=root_fd)
        except OSError:
            raise _unsafe("lock_file_open_failed") from None
    except OSError:
        raise _unsafe("lock_file_create_failed") from None

    try:
        if created:
            try:
                os.fchmod(descriptor, 0o600)
            except OSError:
                raise _unsafe("lock_file_mode_failed") from None
        info = _fstat(descriptor)
        _validate_file(info, private=True)
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise _unsafe("lock_file_mode_invalid")
        named = _stat_at(root_fd, DOCUMENT_LOCK_FILE)
        if named is None or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
            raise _unsafe("lock_file_replaced")
        if created:
            _fsync_file(descriptor)
            _fsync_directory(root_fd, before_commit=True)
        return descriptor
    except BaseException:
        _close_descriptor(descriptor)
        raise


def _acquire_lock(descriptor: int, *, exclusive: bool) -> None:
    """Acquire one advisory flock with a stable, bounded failure."""
    assert fcntl is not None
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                if exc.errno in (
                    errno.ENOSYS,
                    errno.EINVAL,
                    getattr(errno, "ENOTSUP", -1),
                    getattr(errno, "EOPNOTSUPP", -1),
                ):
                    raise _unsupported("flock_unsupported") from None
                raise EntityFileStoreError(
                    "document_lock_unavailable", "lock_acquire_failed"
                ) from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EntityFileStoreError(
                    "document_lock_unavailable", "lock_wait_timeout"
                ) from None
            time.sleep(min(_LOCK_POLL_SECONDS, remaining))


def _read_file_at(
    parent_fd: int,
    name: str,
    *,
    maximum_bytes: int,
    missing_code: str,
    private: bool = False,
) -> Tuple[bytes, Tuple[int, int]]:
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise EntityFileStoreError(missing_code, "referenced_file_missing") from None
    except OSError:
        raise _unsafe("file_open_failed") from None
    try:
        before = _fstat(descriptor)
        _validate_file(before, private=private)
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            raise EntityFileStoreError(
                "document_storage_layout_invalid", "file_size_invalid"
            )
        chunks = []
        remaining = before.st_size
        while remaining:
            try:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
            except OSError:
                raise EntityFileStoreError(
                    "project_recovery_required", "file_read_failed"
                ) from None
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = _fstat(descriptor)
        if (
            len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise EntityFileStoreError(
                "project_recovery_required", "file_changed_during_read"
            )
        return payload, (before.st_dev, before.st_ino)
    finally:
        _close_descriptor(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(descriptor, view[offset:])
        except OSError:
            raise EntityFileStoreError(
                "document_write_failed", "file_write_failed"
            ) from None
        if written <= 0:
            raise EntityFileStoreError(
                "document_write_failed", "short_file_write"
            )
        offset += written


def _atomic_write_at(
    parent_fd: int,
    target: str,
    payload: bytes,
    *,
    commit_point: bool = False,
) -> None:
    existing = _stat_at(parent_fd, target)
    if existing is not None:
        _validate_file(existing)

    descriptor = -1
    temp_name = ""
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(8):
        candidate = ".ze-tmp-%s.tmp" % secrets.token_hex(16)
        try:
            descriptor = os.open(candidate, flags, 0o600, dir_fd=parent_fd)
            temp_name = candidate
            break
        except FileExistsError:
            continue
        except OSError:
            raise _unsafe("temporary_file_create_failed") from None
    if descriptor < 0:
        raise _unsafe("temporary_file_collision")

    moved = False
    try:
        info = _fstat(descriptor)
        _validate_file(info, private=True)
        _write_all(descriptor, payload)
        _fsync_file(descriptor)
        name_info = _stat_at(parent_fd, temp_name)
        if name_info is None or (name_info.st_dev, name_info.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise _unsafe("temporary_file_replaced")
        try:
            os.close(descriptor)
        except OSError:
            descriptor = -1
            raise EntityFileStoreError(
                "document_write_failed", "file_close_failed"
            ) from None
        descriptor = -1
        try:
            os.rename(
                temp_name,
                target,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except OSError:
            raise EntityFileStoreError(
                "document_write_failed", "atomic_replace_failed"
            ) from None
        moved = True
        # Entity generation files are preparatory: a failed directory fsync
        # leaves the authoritative document head unchanged.  document.json is
        # different.  Once its rename succeeds the visible head has advanced,
        # even when the following durability barrier cannot be confirmed.
        _fsync_directory(parent_fd, before_commit=not commit_point)
    finally:
        if descriptor >= 0:
            _close_descriptor(descriptor)
        if temp_name and not moved:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _remove_entry_at(parent_fd: int, name: str, *, depth: int = 0) -> None:
    if depth > 8:
        raise EntityFileStoreError(
            "document_storage_layout_invalid", "tree_depth_invalid"
        )
    info = _stat_at(parent_fd, name)
    if info is None:
        return
    if stat.S_ISREG(info.st_mode):
        _validate_file(info)
        try:
            os.unlink(name, dir_fd=parent_fd)
        except OSError:
            raise EntityFileStoreError(
                "document_write_failed", "cleanup_unlink_failed"
            ) from None
        return
    if not stat.S_ISDIR(info.st_mode):
        raise _unsafe("cleanup_entry_unsafe")
    child_fd = _open_directory_at(parent_fd, name)
    try:
        for child in _list_directory(child_fd):
            _remove_entry_at(child_fd, child, depth=depth + 1)
        _fsync_directory(child_fd, before_commit=True)
    finally:
        _close_descriptor(child_fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        raise EntityFileStoreError(
            "document_write_failed", "cleanup_rmdir_failed"
        ) from None


class EntityFileStore:
    """One held project-directory descriptor for Document save/open."""

    def __init__(
        self,
        document_path: str | os.PathLike[str],
        *,
        create_parent: bool,
        _expected_root_identity: tuple[int, int] | None = None,
    ) -> None:
        _require_platform()
        if _expected_root_identity is not None and (
            type(_expected_root_identity) is not tuple
            or len(_expected_root_identity) != 2
            or any(
                type(member) is not int or member < 0
                for member in _expected_root_identity
            )
        ):
            raise _unsafe("expected_root_identity_invalid")
        absolute = Path(os.path.abspath(os.fspath(document_path)))
        if not absolute.name or absolute.name in (".", "..") or "\x00" in absolute.name:
            raise _unsafe("document_name_invalid")
        self.document_path = absolute
        self.document_name = absolute.name
        self._root_fd = self._open_root(create_parent=create_parent)
        try:
            if _expected_root_identity is not None:
                opened_root = _fstat(self._root_fd)
                if (opened_root.st_dev, opened_root.st_ino) != _expected_root_identity:
                    raise _unsafe("project_directory_identity_mismatch")
        except BaseException:
            _close_descriptor(self._root_fd)
            self._root_fd = -1
            raise
        self._lock_fd = -1
        self._lock_mode: str | None = None

    def _open_root(self, *, create_parent: bool) -> int:
        try:
            descriptor = _open_existing_ancestor(self.document_path.parent)
        except FileNotFoundError:
            if not create_parent:
                raise _unsafe("project_directory_unavailable") from None
        else:
            try:
                _validate_user_ancestor_chain(self.document_path.parent)
            except BaseException:
                _close_descriptor(descriptor)
                raise
            return descriptor

        # Preserve the legacy ``mkdir(parents=True)`` capability without ever
        # traversing a missing/untrusted suffix by pathname.  Find the nearest
        # existing owner-controlled ancestor, then create and re-open every
        # missing component relative to one held descriptor.  A symlink raced
        # into the suffix is rejected by O_NOFOLLOW.  A root-owned sticky shared
        # temp directory (standard /tmp) is an explicit trust anchor; writable
        # non-sticky or non-root shared ancestors remain rejected.
        missing: list[str] = []
        cursor = self.document_path.parent
        while True:
            component = cursor.name
            if not component or component in (".", "..") or "\x00" in component:
                raise _unsafe("project_directory_name_invalid")
            missing.append(component)
            cursor = cursor.parent
            try:
                current_fd = _open_existing_ancestor(cursor)
            except FileNotFoundError:
                continue
            try:
                _validate_user_ancestor_chain(cursor)
            except BaseException:
                _close_descriptor(current_fd)
                raise
            break

        try:
            for component in reversed(missing):
                created = False
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                    created = True
                    _fsync_directory(current_fd, before_commit=True)
                except FileExistsError:
                    # A cooperative creator may have won the race.  The next
                    # openat+fstat still has to prove an owner-safe directory.
                    pass
                except OSError:
                    raise _unsafe("project_directory_create_failed") from None
                next_fd = _open_directory_at(
                    current_fd,
                    component,
                    private=created,
                )
                _close_descriptor(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            _close_descriptor(current_fd)
            raise

    def close(self) -> None:
        if self._lock_fd >= 0:
            _close_descriptor(self._lock_fd)
            self._lock_fd = -1
            self._lock_mode = None
        if self._root_fd >= 0:
            _close_descriptor(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> "EntityFileStore":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def acquire_shared_lock(self) -> None:
        self._acquire_project_lock(exclusive=False)

    def acquire_exclusive_lock(self) -> None:
        self._acquire_project_lock(exclusive=True)

    def _acquire_project_lock(self, *, exclusive: bool) -> None:
        if self._root_fd < 0:
            raise EntityFileStoreError(
                "document_lock_unavailable", "store_closed"
            )
        if self._lock_fd >= 0:
            raise EntityFileStoreError(
                "document_lock_unavailable", "lock_already_held"
            )
        descriptor = _open_lock_file(self._root_fd)
        try:
            _acquire_lock(descriptor, exclusive=exclusive)
            # Re-prove that future peers opening the stable name will lock the
            # same inode.  A same-uid malicious replacement is outside the
            # threat model, but a cooperative unlink/race is still rejected.
            named = _stat_at(self._root_fd, DOCUMENT_LOCK_FILE)
            info = _fstat(descriptor)
            if named is None or (named.st_dev, named.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise _unsafe("lock_file_replaced")
        except BaseException:
            _close_descriptor(descriptor)
            raise
        self._lock_fd = descriptor
        self._lock_mode = "exclusive" if exclusive else "shared"

    def _require_lock(self, *, exclusive: bool) -> None:
        if self._lock_fd < 0 or self._lock_mode not in ("shared", "exclusive"):
            raise EntityFileStoreError(
                "document_lock_unavailable", "shared_lock_required"
            )
        if exclusive and self._lock_mode != "exclusive":
            raise EntityFileStoreError(
                "document_lock_unavailable", "exclusive_lock_required"
            )

    def read_document(self) -> bytes:
        self._require_lock(exclusive=False)
        payload, _identity = _read_file_at(
            self._root_fd,
            self.document_name,
            maximum_bytes=MAX_DOCUMENT_FILE_BYTES,
            missing_code="project_recovery_required",
        )
        return payload

    def read_document_if_exists(self) -> bytes | None:
        """Read the authoritative head, or return None for a new project."""
        self._require_lock(exclusive=False)
        info = _stat_at(self._root_fd, self.document_name)
        if info is None:
            return None
        _validate_file(info)
        return self.read_document()

    def audit_before_save(self) -> None:
        self._require_lock(exclusive=True)
        document_info = _stat_at(self._root_fd, self.document_name)
        if document_info is not None:
            _validate_file(document_info)
        entities_fd = self._open_entities(create=False)
        if entities_fd is None:
            return
        try:
            count = [0]
            for name in _list_directory(entities_fd):
                count[0] += 1
                if count[0] > MAX_TREE_ENTRIES:
                    raise EntityFileStoreError(
                        "document_storage_layout_invalid", "tree_entry_limit_exceeded"
                    )
                if name == ENTITY_STORE_DIRECTORY:
                    self._audit_v2_store(entities_fd, name, count)
                else:
                    require_legacy_type_component(name)
                    self._audit_legacy_type(entities_fd, name, count)
        finally:
            _close_descriptor(entities_fd)

    def _audit_legacy_type(self, entities_fd: int, name: str, count: list[int]) -> None:
        type_fd = _open_directory_at(entities_fd, name)
        try:
            for entry in _list_directory(type_fd):
                count[0] += 1
                if count[0] > MAX_TREE_ENTRIES:
                    raise EntityFileStoreError(
                        "document_storage_layout_invalid", "tree_entry_limit_exceeded"
                    )
                if not entry.endswith(".json"):
                    raise EntityFileStoreError(
                        "document_storage_layout_invalid", "legacy_filename_invalid"
                    )
                require_legacy_component(entry[:-5])
                info = _stat_at(type_fd, entry)
                if info is None:
                    continue
                _validate_file(info)
        finally:
            _close_descriptor(type_fd)

    def _audit_v2_store(self, entities_fd: int, name: str, count: list[int]) -> None:
        store_fd = _open_directory_at(entities_fd, name, private=True)
        try:
            for generation in _list_directory(store_fd):
                count[0] += 1
                if count[0] > MAX_TREE_ENTRIES:
                    raise EntityFileStoreError(
                        "document_storage_layout_invalid", "tree_entry_limit_exceeded"
                    )
                if (
                    not generation.isdigit()
                    or generation.startswith("0")
                    or int(generation) > MAX_DOCUMENT_VERSION
                ):
                    raise EntityFileStoreError(
                        "document_storage_layout_invalid", "generation_name_invalid"
                    )
                generation_fd = _open_directory_at(
                    store_fd, generation, private=True
                )
                try:
                    for shard in _list_directory(generation_fd):
                        count[0] += 1
                        if count[0] > MAX_TREE_ENTRIES:
                            raise EntityFileStoreError(
                                "document_storage_layout_invalid",
                                "tree_entry_limit_exceeded",
                            )
                        if not _SHARD.fullmatch(shard):
                            raise EntityFileStoreError(
                                "document_storage_layout_invalid", "shard_name_invalid"
                            )
                        shard_fd = _open_directory_at(
                            generation_fd, shard, private=True
                        )
                        try:
                            for filename in _list_directory(shard_fd):
                                count[0] += 1
                                if count[0] > MAX_TREE_ENTRIES:
                                    raise EntityFileStoreError(
                                        "document_storage_layout_invalid",
                                        "tree_entry_limit_exceeded",
                                    )
                                if _TEMP.fullmatch(filename):
                                    info = _stat_at(shard_fd, filename)
                                    if info is not None:
                                        _validate_file(info, private=True)
                                        try:
                                            os.unlink(filename, dir_fd=shard_fd)
                                        except OSError:
                                            raise EntityFileStoreError(
                                                "document_write_failed",
                                                "stale_temporary_cleanup_failed",
                                            ) from None
                                    continue
                                if not filename.endswith(".json"):
                                    raise EntityFileStoreError(
                                        "document_storage_layout_invalid",
                                        "entity_filename_invalid",
                                    )
                                digest = filename[:-5]
                                if not _DIGEST.fullmatch(digest) or digest[:2] != shard:
                                    raise EntityFileStoreError(
                                        "document_storage_layout_invalid",
                                        "entity_filename_invalid",
                                    )
                                info = _stat_at(shard_fd, filename)
                                if info is not None:
                                    _validate_file(info, private=True)
                        finally:
                            _close_descriptor(shard_fd)
                finally:
                    _close_descriptor(generation_fd)
        finally:
            _close_descriptor(store_fd)

    def _open_entities(self, *, create: bool) -> int | None:
        try:
            return _open_directory_at(self._root_fd, "entities", create=create)
        except FileNotFoundError:
            return None

    def write_generation(self, version: int, payloads: Dict[str, bytes]) -> None:
        self._require_lock(exclusive=True)
        if type(version) is not int or version < 1 or version > MAX_DOCUMENT_VERSION:
            raise EntityFileStoreError(
                "document_storage_layout_invalid", "generation_invalid"
            )
        if type(payloads) is not dict:
            raise TypeError("payloads must be a dict")
        for digest, payload in payloads.items():
            if type(digest) is not str or not _DIGEST.fullmatch(digest):
                raise EntityFileStoreError(
                    "document_storage_layout_invalid", "digest_invalid"
                )
            if type(payload) is not bytes or not payload or len(payload) > MAX_ENTITY_FILE_BYTES:
                raise EntityFileStoreError(
                    "document_storage_layout_invalid", "entity_payload_size_invalid"
                )

        entities_fd = self._open_entities(create=True)
        assert entities_fd is not None
        try:
            store_fd = _open_directory_at(
                entities_fd, ENTITY_STORE_DIRECTORY, create=True, private=True
            )
            try:
                generation_name = str(version)
                if _stat_at(store_fd, generation_name) is not None:
                    _remove_entry_at(store_fd, generation_name)
                    _fsync_directory(store_fd, before_commit=True)
                generation_fd = _open_directory_at(
                    store_fd, generation_name, create=True, private=True
                )
                try:
                    shard_fds: Dict[str, int] = {}
                    try:
                        for digest, payload in payloads.items():
                            shard = digest[:2]
                            shard_fd = shard_fds.get(shard)
                            if shard_fd is None:
                                shard_fd = _open_directory_at(
                                    generation_fd,
                                    shard,
                                    create=True,
                                    private=True,
                                )
                                shard_fds[shard] = shard_fd
                            _atomic_write_at(shard_fd, digest + ".json", payload)
                    finally:
                        for descriptor in shard_fds.values():
                            _fsync_directory(descriptor, before_commit=True)
                            _close_descriptor(descriptor)
                    _fsync_directory(generation_fd, before_commit=True)
                finally:
                    _close_descriptor(generation_fd)
                _fsync_directory(store_fd, before_commit=True)
            finally:
                _close_descriptor(store_fd)
            _fsync_directory(entities_fd, before_commit=True)
        finally:
            _close_descriptor(entities_fd)

    def commit_document(self, payload: bytes) -> None:
        self._require_lock(exclusive=True)
        if type(payload) is not bytes or not payload or len(payload) > MAX_DOCUMENT_FILE_BYTES:
            raise EntityFileStoreError(
                "document_storage_layout_invalid", "document_payload_size_invalid"
            )
        _atomic_write_at(
            self._root_fd,
            self.document_name,
            payload,
            commit_point=True,
        )

    def read_v2_entity(
        self,
        version: int,
        entity_type: str,
        entity_id: str,
        *,
        _maximum_bytes: int | None = None,
    ) -> bytes:
        self._require_lock(exclusive=False)
        if _maximum_bytes is not None and (
            type(_maximum_bytes) is not int or _maximum_bytes < 0
        ):
            raise TypeError("_maximum_bytes must be an exact non-negative integer")
        maximum_bytes = (
            MAX_ENTITY_FILE_BYTES
            if _maximum_bytes is None
            else min(MAX_ENTITY_FILE_BYTES, _maximum_bytes)
        )
        digest = entity_identity_digest(entity_type, entity_id)
        entities_fd = self._open_entities(create=False)
        if entities_fd is None:
            raise EntityFileStoreError("entity_file_missing", "entities_missing")
        try:
            store_fd = _open_directory_at(
                entities_fd, ENTITY_STORE_DIRECTORY, private=True
            )
            try:
                generation_fd = _open_directory_at(
                    store_fd, str(version), private=True
                )
                try:
                    shard_fd = _open_directory_at(
                        generation_fd, digest[:2], private=True
                    )
                    try:
                        payload, _identity = _read_file_at(
                            shard_fd,
                            digest + ".json",
                            maximum_bytes=maximum_bytes,
                            missing_code="entity_file_missing",
                            private=True,
                        )
                        return payload
                    finally:
                        _close_descriptor(shard_fd)
                finally:
                    _close_descriptor(generation_fd)
            finally:
                _close_descriptor(store_fd)
        except FileNotFoundError:
            raise EntityFileStoreError("entity_file_missing", "entity_file_missing") from None
        finally:
            _close_descriptor(entities_fd)

    def read_legacy_entity(
        self,
        entity_type: str,
        entity_id: str,
        *,
        _maximum_bytes: int | None = None,
    ) -> Tuple[bytes, Tuple[int, int]]:
        self._require_lock(exclusive=False)
        if _maximum_bytes is not None and (
            type(_maximum_bytes) is not int or _maximum_bytes < 0
        ):
            raise TypeError("_maximum_bytes must be an exact non-negative integer")
        maximum_bytes = (
            MAX_ENTITY_FILE_BYTES
            if _maximum_bytes is None
            else min(MAX_ENTITY_FILE_BYTES, _maximum_bytes)
        )
        type_component = require_legacy_type_component(entity_type)
        id_component = require_legacy_component(entity_id)
        entities_fd = self._open_entities(create=False)
        if entities_fd is None:
            raise EntityFileStoreError("entity_file_missing", "entities_missing")
        try:
            type_fd = _open_directory_at(entities_fd, type_component)
            try:
                return _read_file_at(
                    type_fd,
                    id_component + ".json",
                    maximum_bytes=maximum_bytes,
                    missing_code="entity_file_missing",
                )
            finally:
                _close_descriptor(type_fd)
        except FileNotFoundError:
            raise EntityFileStoreError("entity_file_missing", "entity_file_missing") from None
        finally:
            _close_descriptor(entities_fd)

    def cleanup_after_commit(self, current_version: int) -> bool:
        """Remove unreachable layouts; never follow an unsafe entry after commit."""
        self._require_lock(exclusive=True)
        try:
            entities_fd = self._open_entities(create=False)
            if entities_fd is None:
                return True
            try:
                for name in _list_directory(entities_fd):
                    if name != ENTITY_STORE_DIRECTORY:
                        _remove_entry_at(entities_fd, name)
                        continue
                    store_fd = _open_directory_at(
                        entities_fd, name, private=True
                    )
                    try:
                        for generation in _list_directory(store_fd):
                            if generation != str(current_version):
                                _remove_entry_at(store_fd, generation)
                        _fsync_directory(store_fd, before_commit=True)
                    finally:
                        _close_descriptor(store_fd)
                _fsync_directory(entities_fd, before_commit=True)
            finally:
                _close_descriptor(entities_fd)
            return True
        except EntityFileStoreError:
            # The new document already names a complete generation.  Garbage is
            # non-authoritative; leaving it is safer than turning a durable head
            # into an ambiguous failed save or following an unsafe entry.
            return False


__all__ = [
    "DOCUMENT_LOCK_FILE",
    "ENTITY_STORAGE_LAYOUT",
    "ENTITY_STORAGE_SCHEMA",
    "ENTITY_STORE_DIRECTORY",
    "EntityFileStore",
    "EntityFileStoreError",
    "entity_identity_digest",
    "entity_storage_marker",
    "is_entity_storage_marker",
    "require_legacy_component",
    "require_legacy_type_component",
]
