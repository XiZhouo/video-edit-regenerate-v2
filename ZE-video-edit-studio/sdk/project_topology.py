"""Crash-recoverable project namespace transitions for the file compatibility backend.

This module owns the pieces that must never be reimplemented by individual
Repository methods: stable cross-process locks, generation fencing, unique
staging/trash paths, durable topology journals, immutable outcome receipts, and
namespace rename recovery.
Business document construction, ACL policy, and scenario semantics remain in
``sdk.bootstrap.Repository`` and enter through callbacks.

The implementation targets one host with a local filesystem and ``flock``.
W06 replaces its catalog/journal with a transactional database; callers keep the
same generation/activity contract.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import _paths  # noqa: F401 - inject leaf modules before importing ZeError
from document import (  # noqa: E402
    DocumentHead,
    DocumentMetadata,
    _TOPOLOGY_STAGING_METADATA_CAPABILITY,
    _rewrite_document_metadata_for_topology_staging,
    open_document_head,
)
from entity import ZeError  # noqa: E402

try:  # pragma: no cover - platform branch; production currently runs on Unix
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


PROJECT_MARKER = ".ze-project.json"
STATE_SCHEMA = "ze.project_state.v1"
MARKER_SCHEMA = "ze.project_marker.v1"
JOURNAL_SCHEMA = "ze.project_topology_journal.v1"
OUTCOME_SCHEMA = "ze.project_topology_outcome.v1"
PRINCIPAL_CLAIM_SCHEMA = "ze.project_topology_principal.v1"
REPOSITORY_ADMISSION_SCHEMA = "ze.repository_admission.v1"
REPOSITORY_ADMISSION_NAME = "repository-admission.json"
_REPOSITORY_ADMISSION_INSTALL_NAME = ".repository-admission-installed-v1"
_REPOSITORY_ADMISSION_INSTALL_SCHEMA = "ze.repository_admission_install.v1"
_REPOSITORY_ADMISSION_INSTALL_PHASES = frozenset(("prepared", "committed"))
_REPOSITORY_ADMISSION_INSTALL_MODES = frozenset(("fresh", "legacy_bootstrap"))
_REPOSITORY_CUTOVER_FREEZE_NAME = ".repository-cutover-freeze-v1.json"
_REPOSITORY_CUTOVER_FREEZE_SCHEMA = "ze.repository_cutover_freeze.v1"
CONTROL_NAMES = frozenset((".ze-staging", ".ze-trash"))
TOPOLOGY_KINDS = frozenset(("create", "duplicate", "replace", "delete", "legacy_migrate"))
DEFAULT_DURABLE_ENTRIES = ("document.json", "entities", "assets")
_COPY_CHUNK_BYTES = 1024 * 1024
_DURABLE_METADATA_MAX_BYTES = 64 * 1024 * 1024
_COPY_MAX_DEPTH = 64
_COPY_MAX_ENTRIES = 100_000
_COPY_MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
_MAX_PROJECT_GENERATION = 2**63 - 1
_REPOSITORY_ADMISSION_MAX_BYTES = 4096
_REPOSITORY_CUTOVER_FREEZE_MAX_BYTES = 4096
_REPOSITORY_CUTOVER_RUNTIME_GENERATION_MAX_BYTES = 256
_REPOSITORY_AUTHORITY_STATES = frozenset((
    "legacy", "cutover_pending", "sqlite",
))
_REPOSITORY_ADMISSION_CAPABILITY = object()
_REPOSITORY_SQLITE_AUTHORITY_CAPABILITY = object()
_REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY = object()
_REPOSITORY_CUTOVER_FREEZE_CAPABILITY = object()
_LIVE_SCAN_SOURCE_SEAL = object()
_UNSUPPORTED_DIR_FSYNC = frozenset(filter(None, (
    errno.EINVAL,
    getattr(errno, "ENOTSUP", None),
    getattr(errno, "EOPNOTSUPP", None),
    getattr(errno, "EBADF", None),
)))
_FALLBACK_LOCKS: dict[str, threading.RLock] = {}
_FALLBACK_LOCKS_GUARD = threading.Lock()
_HELD_LOCKS = threading.local()
_STRICT_REPOSITORY_MIGRATION_SCOPE = threading.local()
_HELD_LOCK_STREAMS: dict[int, Any] = {}
_HELD_LOCK_STREAMS_GUARD = threading.RLock()


Build = Callable[[Path], None]
Rewrite = Callable[[Path], None]
Validate = Callable[[Path], None]
VerifyExisting = Callable[[Path], None]
Quiesce = Callable[[float], bool]


@dataclass(frozen=True)
class ProjectHandle:
    project_id: str
    path: Path
    generation: int


@dataclass(frozen=True, init=False)
class _LiveScanSource:
    """Framework-issued live source whose root and content locks remain held."""

    project_id: str
    generation: int
    document_path: Path
    root_identity: tuple[int, int]
    head: DocumentHead = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        project_id: str,
        generation: int,
        document_path: Path,
        root_identity: tuple[int, int],
        head: DocumentHead,
        _seal: Any = None,
    ) -> None:
        if _seal is not _LIVE_SCAN_SOURCE_SEAL:
            raise TypeError("_LiveScanSource requires framework-issued authority")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "document_path", document_path)
        object.__setattr__(self, "root_identity", root_identity)
        object.__setattr__(self, "head", head)


@dataclass(frozen=True)
class TopologyCommit:
    operation_id: str
    project_id: str
    generation: int
    cleanup_pending: bool = False


@dataclass(frozen=True)
class _RepositoryAdmission:
    """Exact durable file-writer admission state; never a cutover proof."""

    authority_state: str
    authority_epoch: str
    revision: int
    cutover_id: str


@dataclass(frozen=True)
class _RepositoryAdmissionInstallMarker:
    """Crash-recoverable install intent bound to one exact initial record."""

    phase: str
    install_mode: str
    authority_epoch: str
    initial_record_sha256: str


@dataclass(frozen=True)
class _RepositoryCutoverFreeze:
    """Durable source freeze installed before any cutover shadow write."""

    authority_epoch: str
    source_admission_revision: int
    runtime_generation: str
    code_digest: bytes = field(repr=False)
    freeze_sha256: str


@dataclass(frozen=True)
class _RepositoryStateDescriptor:
    """Path-free immutable input selected by the strict repository barrier."""

    project_id: str
    generation: int
    status: str
    operation_id: str
    source_bytes: bytes = field(repr=False)
    source_sha256: bytes


@dataclass(frozen=True)
class _StrictRepositorySnapshot:
    """Private migration carrier.  It contains no filesystem authority."""

    admission: _RepositoryAdmission
    states: tuple[_RepositoryStateDescriptor, ...]


class _PathLockTimeout(TimeoutError):
    pass


def _before_path_locks_fork() -> None:
    # Pair fork itself with the same mutex that spans fd creation+registration
    # and registry removal+close.  The child can therefore inherit either no
    # descriptor or a descriptor already present in the copied registry, never
    # an untracked fd from the narrow os.open/fdopen window.
    _HELD_LOCK_STREAMS_GUARD.acquire()


def _after_parent_path_locks_fork() -> None:
    _HELD_LOCK_STREAMS_GUARD.release()


def _close_registered_held_lock_stream(stream: Any) -> None:
    with _HELD_LOCK_STREAMS_GUARD:
        try:
            if not stream.closed:
                stream.close()
        finally:
            _HELD_LOCK_STREAMS.pop(id(stream), None)


def _reset_path_locks_after_fork() -> None:
    """Drop every inherited child descriptor without unlocking the parent.

    ``flock`` state follows the inherited open-file description.  Calling
    ``LOCK_UN`` in the child would therefore also release the parent's lock.
    Closing only the child's duplicate descriptors is safe: the parent still
    owns its descriptors, while the child must acquire fresh locks and cannot
    re-enter a copied thread-local registry.
    """

    global _FALLBACK_LOCKS
    global _FALLBACK_LOCKS_GUARD
    global _HELD_LOCKS
    global _STRICT_REPOSITORY_MIGRATION_SCOPE
    global _HELD_LOCK_STREAMS
    global _HELD_LOCK_STREAMS_GUARD

    # Do not acquire a possibly inherited locked mutex in the child.  Python
    # invokes this callback synchronously after fork, while only the forking
    # thread exists, so the copied registry is stable to traverse directly.
    inherited = tuple(_HELD_LOCK_STREAMS.values())
    for stream in inherited:
        try:
            stream.close()
        except Exception:
            pass
    _HELD_LOCKS = threading.local()
    _STRICT_REPOSITORY_MIGRATION_SCOPE = threading.local()
    _HELD_LOCK_STREAMS = {}
    _HELD_LOCK_STREAMS_GUARD = threading.RLock()
    _FALLBACK_LOCKS = {}
    _FALLBACK_LOCKS_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_path_locks_fork,
        after_in_parent=_after_parent_path_locks_fork,
        after_in_child=_reset_path_locks_after_fork,
    )


def _fallback_lock(path: Path) -> threading.RLock:
    key = str(path.absolute())
    with _FALLBACK_LOCKS_GUARD:
        return _FALLBACK_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _path_lock(path: Path, *, exclusive: bool, timeout: float | None = None) -> Iterator[None]:
    """Acquire one stable lock file; topology lock files never move with a project."""
    owner_pid = os.getpid()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.absolute())
    held = getattr(_HELD_LOCKS, "entries", None)
    if held is None:
        held = {}
        _HELD_LOCKS.entries = held
    current = held.get(key)
    if current is not None:
        if exclusive and not current["exclusive"]:
            raise ZeError(
                "project_lock_order_invalid",
                "cannot upgrade a shared project lock in the same thread",
                context={"lock": path.name},
            )
        current["depth"] += 1
        try:
            yield
        finally:
            current["depth"] -= 1
        return

    fallback: threading.RLock | None = None
    stream = None
    if fcntl is None:
        if os.environ.get("ZE_ALLOW_SINGLE_PROCESS_TOPOLOGY", "").strip() != "1":
            raise ZeError(
                "project_topology_unavailable",
                "project topology requires flock on this platform",
                context={"single_process_override": False},
            )
        fallback = _fallback_lock(path)
        acquired = fallback.acquire(timeout=timeout if timeout is not None else -1)
        if not acquired:
            raise _PathLockTimeout(str(path))
    try:
        # Fork's ``before`` callback acquires this same guard.  Keep it across
        # os.open -> fstat -> fdopen -> registry insertion so there is no child
        # process in which an inherited topology fd is absent from the cleanup
        # registry.
        with _HELD_LOCK_STREAMS_GUARD:
            flags = (
                os.O_RDWR | os.O_CREAT | os.O_APPEND
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                fd = os.open(str(path), flags, 0o600)
            except OSError as exc:
                raise ZeError(
                    "project_topology_unavailable",
                    "project topology lock path is unsafe or unavailable",
                    context={"lock": path.name},
                ) from exc
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ZeError(
                        "project_topology_unavailable",
                        "project topology lock must be a regular file",
                        context={"lock": path.name},
                    )
                stream = os.fdopen(fd, "a+b")
                fd = -1
                _HELD_LOCK_STREAMS[id(stream)] = stream
            finally:
                if fd >= 0:
                    os.close(fd)
        if fcntl is not None:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            if timeout is None:
                fcntl.flock(stream.fileno(), mode)
            else:
                deadline = time.monotonic() + max(0.0, timeout)
                while True:
                    try:
                        fcntl.flock(stream.fileno(), mode | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise _PathLockTimeout(str(path))
                        time.sleep(0.02)
        held[key] = {
            "exclusive": exclusive,
            "depth": 1,
            "stream": stream,
            "pid": owner_pid,
        }
        try:
            yield
        finally:
            held.pop(key, None)
            if (
                fcntl is not None
                and os.getpid() == owner_pid
                and not stream.closed
            ):
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        if stream is not None:
            _close_registered_held_lock_stream(stream)
        if fallback is not None:
            fallback.release()


def _fsync_directory(path: Path) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIR_FSYNC:
            return False
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno in _UNSUPPORTED_DIR_FSYNC:
                return False
            raise
    finally:
        os.close(fd)
    return True


def _atomic_json(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    temp = Path(raw_temp)
    try:
        try:
            os.fchmod(fd, mode)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        return _fsync_directory(path.parent)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ZeError(
            "project_recovery_required",
            "project topology metadata is invalid",
            context={"metadata": path.name, "cause_type": type(exc).__name__},
        ) from exc
    if not isinstance(value, dict):
        raise ZeError(
            "project_recovery_required",
            "project topology metadata must be an object",
            context={"metadata": path.name},
        )
    return value


def _repository_admission_error(reason: str) -> ZeError:
    return ZeError(
        "project_recovery_required",
        "repository writer admission record is invalid",
        context={"reason": reason},
    )


def _reject_duplicate_json_members(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, member in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = member
    return value


def _is_exact_lower_hex(value: Any, length: int) -> bool:
    return bool(
        type(value) is str
        and len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )


def _admission_value(admission: _RepositoryAdmission) -> dict[str, Any]:
    return {
        "schema": REPOSITORY_ADMISSION_SCHEMA,
        "authority_state": admission.authority_state,
        "authority_epoch": admission.authority_epoch,
        "revision": admission.revision,
        "cutover_id": admission.cutover_id,
    }


def _repository_admission_digest(admission: _RepositoryAdmission) -> str:
    canonical = json.dumps(
        _admission_value(admission),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(
        b"ze.repository-admission.install-binding.v1\0" + canonical,
    ).hexdigest()


def _admission_install_marker_value(
    marker: _RepositoryAdmissionInstallMarker,
) -> dict[str, Any]:
    return {
        "schema": _REPOSITORY_ADMISSION_INSTALL_SCHEMA,
        "phase": marker.phase,
        "install_mode": marker.install_mode,
        "authority_epoch": marker.authority_epoch,
        "initial_record_sha256": marker.initial_record_sha256,
    }


def _cutover_freeze_identity_value(
    *,
    authority_epoch: str,
    source_admission_revision: int,
    runtime_generation: str,
    code_digest: bytes,
) -> dict[str, Any]:
    return {
        "schema": _REPOSITORY_CUTOVER_FREEZE_SCHEMA,
        "authority_epoch": authority_epoch,
        "source_admission_revision": source_admission_revision,
        "runtime_generation": runtime_generation,
        "code_digest": code_digest.hex(),
    }


def _repository_cutover_freeze_digest(
    *,
    authority_epoch: str,
    source_admission_revision: int,
    runtime_generation: str,
    code_digest: bytes,
) -> str:
    canonical = json.dumps(
        _cutover_freeze_identity_value(
            authority_epoch=authority_epoch,
            source_admission_revision=source_admission_revision,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
        ),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(
        b"ze.repository-cutover-freeze.binding.v1\0" + canonical,
    ).hexdigest()


def _cutover_freeze_value(
    marker: _RepositoryCutoverFreeze,
) -> dict[str, Any]:
    value = _cutover_freeze_identity_value(
        authority_epoch=marker.authority_epoch,
        source_admission_revision=marker.source_admission_revision,
        runtime_generation=marker.runtime_generation,
        code_digest=marker.code_digest,
    )
    value["freeze_sha256"] = marker.freeze_sha256
    return value


def _unlink_durable(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _remove_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ZeError(
            "project_recovery_required",
            "refusing to follow a topology control symlink",
            context={"entry": path.name},
        )
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_tree_durable(path: Path) -> None:
    parent = path.parent
    _remove_tree(path)
    _fsync_directory(parent)


def _is_regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _secure_directory_flags() -> int:
    """Return the flags required by the topology copy trust boundary.

    Copying a project is a security boundary, not a convenience-file-copy
    operation.  Silently dropping ``O_NOFOLLOW``/``O_DIRECTORY`` on a platform
    that lacks them would turn a supposedly checked source into a symlink race,
    so such a platform is unsupported instead of receiving a weaker fallback.
    """
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise ZeError(
            "project_topology_unavailable",
            "secure project copying is unavailable on this platform",
            context={"capability": "dirfd_nofollow"},
        )
    return (
        os.O_RDONLY | nofollow | directory
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    )


def _secure_regular_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ZeError(
            "project_topology_unavailable",
            "secure project copying is unavailable on this platform",
            context={"capability": "dirfd_nofollow"},
        )
    return (
        os.O_RDONLY | nofollow
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    )


def _live_scan_root_error(reason: str, message: str) -> ZeError:
    """Build a path-free read-side diagnosis for an unsafe live root."""
    return ZeError(
        "project_recovery_required",
        message,
        context={"reason": reason},
    )


def _open_live_scan_root(root: Path) -> tuple[int, tuple[int, int]]:
    """Open, validate, and pin one live project root for a scanner."""
    try:
        flags = _secure_directory_flags()
        descriptor = os.open(os.fspath(Path(root).absolute()), flags)
    except ZeError:
        raise
    except OSError:
        raise _live_scan_root_error(
            "live_scan_root_unsafe",
            "live project root is unsafe or unavailable for scanning",
        ) from None
    try:
        try:
            info = os.fstat(descriptor)
        except OSError:
            raise _live_scan_root_error(
                "live_scan_root_unsafe",
                "live project root cannot be verified for scanning",
            ) from None
        if (
            not stat.S_ISDIR(info.st_mode)
            or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
            or bool(stat.S_IMODE(info.st_mode) & 0o022)
        ):
            raise _live_scan_root_error(
                "live_scan_root_unsafe",
                "live project root is not an owner-controlled directory",
            )
        identity = (info.st_dev, info.st_ino)
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _assert_live_scan_root_named(
    root: Path,
    root_identity: tuple[int, int],
) -> None:
    """Prove the pinned directory still owns the public live path."""
    try:
        named = Path(root).lstat()
    except OSError:
        raise _live_scan_root_error(
            "live_scan_root_identity_mismatch",
            "live project root changed during scanning",
        ) from None
    if (
        not stat.S_ISDIR(named.st_mode)
        or stat.S_ISLNK(named.st_mode)
        or (named.st_dev, named.st_ino) != root_identity
    ):
        raise _live_scan_root_error(
            "live_scan_root_identity_mismatch",
            "live project root changed during scanning",
        )
    if (
        (hasattr(os, "geteuid") and named.st_uid != os.geteuid())
        or bool(stat.S_IMODE(named.st_mode) & 0o022)
    ):
        raise _live_scan_root_error(
            "live_scan_root_unsafe",
            "live project root became unsafe during scanning",
        )


@contextmanager
def _open_live_scan_head(
    document_path: Path,
    root_identity: tuple[int, int],
) -> Iterator[DocumentHead]:
    """Bind the content SH lock to the topology-pinned directory inode."""
    entered = False
    try:
        with open_document_head(
            document_path,
            _expected_root_identity=root_identity,
        ) as head:
            assert head is not None
            entered = True
            yield head
    except ZeError as exc:
        if (
            not entered
            and exc.code == "document_storage_path_unsafe"
            and exc.context.get("reason") == "project_directory_identity_mismatch"
        ):
            raise _live_scan_root_error(
                "live_scan_root_identity_mismatch",
                "live project root changed before its content lock was acquired",
            ) from None
        raise


def _copy_error(
    message: str,
    *,
    reason: str = "unsafe_durable_source_entry",
    entry: str = "",
) -> ZeError:
    """Build a public copy error without exposing an entity/asset path.

    ``entry`` remains an accepted internal-only diagnostic argument so callers
    cannot accidentally interpolate an attacker-controlled filename into the
    public context.  The stable ``reason`` is the complete external diagnostic.
    """
    del entry
    return ZeError("project_topology_write_failed", message, context={"reason": reason})


@dataclass
class _CopyBudget:
    entries: int = 0
    total_bytes: int = 0

    def admit(self, info: os.stat_result, *, depth: int) -> None:
        if depth > _COPY_MAX_DEPTH:
            raise _copy_error(
                "project source exceeds the copy depth budget",
                reason="durable_source_depth_exceeded",
            )
        self.entries += 1
        if self.entries > _COPY_MAX_ENTRIES:
            raise _copy_error(
                "project source exceeds the copy entry budget",
                reason="durable_source_entries_exceeded",
            )
        if stat.S_ISREG(info.st_mode):
            self.total_bytes += info.st_size
            if self.total_bytes > _COPY_MAX_TOTAL_BYTES:
                raise _copy_error(
                    "project source exceeds the copy byte budget",
                    reason="durable_source_bytes_exceeded",
                )


def _validate_copy_inode(
    info: os.stat_result,
    *,
    entry: str,
    expected_kind: str,
    require_single_link: bool = False,
) -> None:
    """Validate one inode admitted into a durable project copy.

    Durable sources must belong to the effective user and may be readable by
    others, but may not be group/world writable.  Regular files must have one
    link: accepting a hard link would let a path outside the project mutate the
    bytes selected for publication (and makes later legacy cleanup ambiguous).
    """
    if expected_kind == "directory":
        kind_ok = stat.S_ISDIR(info.st_mode)
    elif expected_kind == "regular":
        kind_ok = stat.S_ISREG(info.st_mode)
    else:  # pragma: no cover - internal programming error
        raise AssertionError(f"unknown copy inode kind: {expected_kind}")
    if not kind_ok:
        raise _copy_error(
            "project source contains a symlink or special file", entry=entry,
        )
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise _copy_error("project source has an unsafe owner", entry=entry)
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise _copy_error("project source has unsafe writable permissions", entry=entry)
    if require_single_link and info.st_nlink != 1:
        raise _copy_error("project source contains a hard-linked file", entry=entry)


def _same_copy_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    """Whether an opened source inode stayed stable while its bytes were read."""
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        return False
    for field in ("st_mtime_ns", "st_ctime_ns"):
        if hasattr(before, field) and getattr(before, field) != getattr(after, field):
            return False
    return True


def _validate_regular_tree(path: Path) -> None:
    """Reject symlinks, sockets, devices, and FIFOs before copying/promoting."""
    if not path.exists():
        return
    root_info = path.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        raise ZeError("project_topology_write_failed", "project entry is not a directory")
    for entry in path.rglob("*"):
        info = entry.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ZeError(
                "project_topology_write_failed",
                "project contains a symlink or special file",
                context={"entry": entry.name},
            )


def _fsync_tree(path: Path) -> None:
    _validate_regular_tree(path)
    directories: list[Path] = []
    for entry in path.rglob("*"):
        info = entry.lstat()
        if stat.S_ISREG(info.st_mode):
            fd = os.open(str(entry), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        elif stat.S_ISDIR(info.st_mode):
            directories.append(entry)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(path)


class ProjectTopologyCoordinator:
    """Own every mutation of the live project namespace."""

    def __init__(
        self,
        projects_root: Path,
        *,
        durable_entries: tuple[str, ...] = DEFAULT_DURABLE_ENTRIES,
        lock_timeout: float = 10.0,
        _repository_admission_bootstrap_capability: Any = None,
        _repository_admission_bootstrap_validate: Validate | None = None,
    ) -> None:
        self.projects_root = Path(projects_root).absolute()
        self.data_root = self.projects_root.parent
        self.control_root = self.data_root / ".ze-topology"
        self.lock_root = self.control_root / "locks"
        self.state_root = self.control_root / "state"
        self.journal_root = self.control_root / "journal"
        self.outcome_root = self.control_root / "outcomes"
        self.intent_root = self.control_root / "intents"
        self.staging_root = self.projects_root / ".ze-staging"
        self.trash_root = self.projects_root / ".ze-trash"
        self.repo_lock = self.control_root / "repo.lock"
        self.catalog_migration_lock = self.lock_root / "catalog-migration.lock"
        self.repository_admission_path = self.control_root / REPOSITORY_ADMISSION_NAME
        self._repository_admission_install_path = (
            self.control_root / _REPOSITORY_ADMISSION_INSTALL_NAME
        )
        self._repository_cutover_freeze_path = (
            self.control_root / _REPOSITORY_CUTOVER_FREEZE_NAME
        )
        try:
            self.control_root.lstat()
            control_preexisting = True
        except FileNotFoundError:
            control_preexisting = False
        cleaned_entries: list[str] = []
        for entry in durable_entries:
            if (
                not isinstance(entry, str)
                or not entry
                or Path(entry).name != entry
                or entry in (".", "..", PROJECT_MARKER, *CONTROL_NAMES)
                or any(char in entry for char in ("/", "\\", "\x00"))
            ):
                raise ValueError(f"invalid durable project entry: {entry!r}")
            cleaned_entries.append(entry)
        self.durable_entries = tuple(cleaned_entries)
        self.lock_timeout = float(lock_timeout)
        self._recovery_required: set[str] = set()
        core_paths = (
            self.data_root,
            self.projects_root,
            self.control_root,
            self.lock_root,
            self.state_root,
            self.journal_root,
            self.outcome_root,
            self.intent_root,
        )
        for path in core_paths:
            try:
                path.lstat()
                _preexisting = True
            except FileNotFoundError:
                _preexisting = False
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not _preexisting:
                # Lock self-built topology roots to owner-only, decoupled from
                # the ambient umask so a lax parent chain or historical 775
                # cannot leak the data/credential roots to the group.
                os.chmod(path, 0o700)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ZeError(
                    "project_topology_unavailable",
                    "project topology directories must be real directories",
                    context={"entry": path.name},
                )
        # Establish the durable writer fence before validating optional project
        # namespace controls.  A constructor that rejects an unsafe staging
        # path still leaves a durable admission record, rather than a partial
        # control root that a later process could mistake for a fresh store.
        for path in (
            self.lock_root, self.state_root, self.journal_root, self.outcome_root,
            self.intent_root,
            self.control_root, self.projects_root, self.data_root,
        ):
            _fsync_directory(path)
        # Strict bootstrap recovery needs both namespace controls even when a
        # previous fresh constructor died before its first admission marker.
        # Create only absent paths here; an existing symlink/special entry is
        # deliberately left untouched and rejected after admission bootstrap
        # (fresh) or by the strict v4 bootstrap proof (existing).
        for path in (self.staging_root, self.trash_root):
            try:
                path.lstat()
            except FileNotFoundError:
                path.mkdir(mode=0o700, parents=True, exist_ok=False)
                os.chmod(path, 0o700)
                _fsync_directory(path)
                _fsync_directory(path.parent)
        self._initialize_repository_admission(
            control_preexisting=control_preexisting,
            bootstrap_capability=_repository_admission_bootstrap_capability,
            bootstrap_validate=_repository_admission_bootstrap_validate,
        )
        for path in (self.staging_root, self.trash_root):
            try:
                path.lstat()
                _preexisting = True
            except FileNotFoundError:
                _preexisting = False
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not _preexisting:
                os.chmod(path, 0o700)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ZeError(
                    "project_topology_unavailable",
                    "project topology directories must be real directories",
                    context={"entry": path.name},
                )
        # Make first-use namespace controls durable before journals rely on them.
        for path in (
            self.staging_root, self.trash_root, self.projects_root, self.data_root,
        ):
            _fsync_directory(path)

    def _validate_admission_parent(self) -> None:
        try:
            info = self.control_root.lstat()
        except OSError:
            raise _repository_admission_error("admission_parent_unavailable") from None
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
            or bool(stat.S_IMODE(info.st_mode) & 0o022)
        ):
            raise _repository_admission_error("admission_parent_unsafe")

    def _validate_repository_namespace_controls(self) -> None:
        """Prove recovery controls cannot redirect traversal outside the store."""

        for path in (self.staging_root, self.trash_root):
            try:
                info = path.lstat()
            except OSError:
                raise _repository_admission_error(
                    "admission_namespace_control_unavailable",
                ) from None
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
                or bool(stat.S_IMODE(info.st_mode) & 0o022)
            ):
                raise _repository_admission_error(
                    "admission_namespace_control_unsafe",
                )

    def _read_repository_admission(
        self,
        *,
        allow_missing: bool = False,
    ) -> _RepositoryAdmission | None:
        """Read one exact owner-controlled record without following links."""

        self._validate_admission_parent()
        flags = (
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise _repository_admission_error("admission_nofollow_unavailable")
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    os.fspath(self.repository_admission_path),
                    flags | nofollow,
                )
            except FileNotFoundError:
                if allow_missing:
                    return None
                raise _repository_admission_error("admission_missing") from None
            except OSError:
                raise _repository_admission_error("admission_path_unsafe") from None
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
                or bool(stat.S_IMODE(before.st_mode) & 0o077)
                or not 0 < before.st_size <= _REPOSITORY_ADMISSION_MAX_BYTES
            ):
                raise _repository_admission_error("admission_inode_unsafe")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    raise _repository_admission_error("admission_truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise _repository_admission_error("admission_oversized")
            after = os.fstat(descriptor)
            compared = (
                "st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size",
                "st_mtime_ns", "st_ctime_ns",
            )
            if any(
                hasattr(before, member)
                and getattr(before, member) != getattr(after, member)
                for member in compared
            ):
                raise _repository_admission_error("admission_changed_during_read")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            raw = json.loads(
                b"".join(chunks).decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_members,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise _repository_admission_error("admission_json_invalid") from None
        if type(raw) is not dict or set(raw) != {
            "schema", "authority_state", "authority_epoch", "revision", "cutover_id",
        }:
            raise _repository_admission_error("admission_shape_invalid")
        authority_state = raw.get("authority_state")
        authority_epoch = raw.get("authority_epoch")
        revision = raw.get("revision")
        cutover_id = raw.get("cutover_id")
        if (
            raw.get("schema") != REPOSITORY_ADMISSION_SCHEMA
            or type(authority_state) is not str
            or authority_state not in _REPOSITORY_AUTHORITY_STATES
            or not _is_exact_lower_hex(authority_epoch, 64)
            or type(revision) is not int
            or not 1 <= revision <= _MAX_PROJECT_GENERATION
            or (
                authority_state == "legacy"
                and cutover_id != ""
            )
            or (
                authority_state in ("cutover_pending", "sqlite")
                and not _is_exact_lower_hex(cutover_id, 64)
            )
        ):
            raise _repository_admission_error("admission_value_invalid")
        return _RepositoryAdmission(
            authority_state=authority_state,
            authority_epoch=authority_epoch,
            revision=revision,
            cutover_id=cutover_id,
        )

    def _write_repository_admission(self, admission: _RepositoryAdmission) -> None:
        self._validate_admission_parent()
        _atomic_json(
            self.repository_admission_path,
            _admission_value(admission),
            mode=0o600,
        )
        observed = self._read_repository_admission()
        if observed != admission:
            raise _repository_admission_error("admission_write_mismatch")

    def _read_repository_admission_install_marker(
        self,
    ) -> _RepositoryAdmissionInstallMarker | None:
        self._validate_admission_parent()
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise _repository_admission_error("admission_nofollow_unavailable")
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    os.fspath(self._repository_admission_install_path),
                    os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                )
            except FileNotFoundError:
                return None
            except OSError:
                raise _repository_admission_error(
                    "admission_install_marker_unsafe",
                ) from None
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
                or bool(stat.S_IMODE(before.st_mode) & 0o077)
                or not 0 < before.st_size <= _REPOSITORY_ADMISSION_MAX_BYTES
            ):
                raise _repository_admission_error(
                    "admission_install_marker_invalid",
                )
            payload = os.read(descriptor, before.st_size + 1)
            after = os.fstat(descriptor)
            if len(payload) != before.st_size or any(
                hasattr(before, member)
                and getattr(before, member) != getattr(after, member)
                for member in (
                    "st_dev", "st_ino", "st_mode", "st_uid", "st_nlink",
                    "st_size", "st_mtime_ns", "st_ctime_ns",
                )
            ):
                raise _repository_admission_error(
                    "admission_install_marker_changed",
                )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            raw = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_members,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise _repository_admission_error(
                "admission_install_marker_json_invalid",
            ) from None
        if type(raw) is not dict or set(raw) != {
            "schema", "phase", "install_mode", "authority_epoch",
            "initial_record_sha256",
        }:
            raise _repository_admission_error(
                "admission_install_marker_shape_invalid",
            )
        phase = raw.get("phase")
        install_mode = raw.get("install_mode")
        authority_epoch = raw.get("authority_epoch")
        initial_record_sha256 = raw.get("initial_record_sha256")
        if (
            raw.get("schema") != _REPOSITORY_ADMISSION_INSTALL_SCHEMA
            or type(phase) is not str
            or phase not in _REPOSITORY_ADMISSION_INSTALL_PHASES
            or type(install_mode) is not str
            or install_mode not in _REPOSITORY_ADMISSION_INSTALL_MODES
            or not _is_exact_lower_hex(authority_epoch, 64)
            or not _is_exact_lower_hex(initial_record_sha256, 64)
        ):
            raise _repository_admission_error(
                "admission_install_marker_value_invalid",
            )
        marker = _RepositoryAdmissionInstallMarker(
            phase=phase,
            install_mode=install_mode,
            authority_epoch=authority_epoch,
            initial_record_sha256=initial_record_sha256,
        )
        self._initial_admission_for_install_marker(marker)
        return marker

    @staticmethod
    def _initial_admission_for_install_marker(
        marker: _RepositoryAdmissionInstallMarker,
    ) -> _RepositoryAdmission:
        initial = _RepositoryAdmission(
            authority_state="legacy",
            authority_epoch=marker.authority_epoch,
            revision=1,
            cutover_id="",
        )
        if _repository_admission_digest(initial) != marker.initial_record_sha256:
            raise _repository_admission_error(
                "admission_install_marker_binding_invalid",
            )
        return initial

    def _write_repository_admission_install_marker(
        self,
        marker: _RepositoryAdmissionInstallMarker,
    ) -> None:
        self._initial_admission_for_install_marker(marker)
        _atomic_json(
            self._repository_admission_install_path,
            _admission_install_marker_value(marker),
            mode=0o600,
        )
        if self._read_repository_admission_install_marker() != marker:
            raise _repository_admission_error(
                "admission_install_marker_write_mismatch",
            )

    def _read_repository_cutover_freeze(
        self,
    ) -> _RepositoryCutoverFreeze | None:
        """Read the exact owner-controlled source freeze without links."""

        self._validate_admission_parent()
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise _repository_admission_error(
                "cutover_freeze_nofollow_unavailable",
            )
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    os.fspath(self._repository_cutover_freeze_path),
                    os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                )
            except FileNotFoundError:
                return None
            except OSError:
                raise _repository_admission_error(
                    "cutover_freeze_path_unsafe",
                ) from None
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
                or bool(stat.S_IMODE(before.st_mode) & 0o077)
                or not 0 < before.st_size <= _REPOSITORY_CUTOVER_FREEZE_MAX_BYTES
            ):
                raise _repository_admission_error(
                    "cutover_freeze_inode_unsafe",
                )
            payload = os.read(descriptor, before.st_size + 1)
            after = os.fstat(descriptor)
            if len(payload) != before.st_size or any(
                hasattr(before, member)
                and getattr(before, member) != getattr(after, member)
                for member in (
                    "st_dev", "st_ino", "st_mode", "st_uid", "st_nlink",
                    "st_size", "st_mtime_ns", "st_ctime_ns",
                )
            ):
                raise _repository_admission_error(
                    "cutover_freeze_changed_during_read",
                )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            raw = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_members,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise _repository_admission_error(
                "cutover_freeze_json_invalid",
            ) from None
        if type(raw) is not dict or set(raw) != {
            "schema", "authority_epoch", "source_admission_revision",
            "runtime_generation", "code_digest", "freeze_sha256",
        }:
            raise _repository_admission_error(
                "cutover_freeze_shape_invalid",
            )
        authority_epoch = raw.get("authority_epoch")
        source_revision = raw.get("source_admission_revision")
        runtime_generation = raw.get("runtime_generation")
        code_digest_hex = raw.get("code_digest")
        freeze_sha256 = raw.get("freeze_sha256")
        try:
            runtime_generation_bytes = (
                runtime_generation.encode("utf-8", "strict")
                if type(runtime_generation) is str
                else b""
            )
        except UnicodeEncodeError:
            runtime_generation_bytes = b""
        if (
            raw.get("schema") != _REPOSITORY_CUTOVER_FREEZE_SCHEMA
            or not _is_exact_lower_hex(authority_epoch, 64)
            or type(source_revision) is not int
            or not 1 <= source_revision <= _MAX_PROJECT_GENERATION - 2
            or type(runtime_generation) is not str
            or not 1 <= len(runtime_generation_bytes)
            <= _REPOSITORY_CUTOVER_RUNTIME_GENERATION_MAX_BYTES
            or not _is_exact_lower_hex(code_digest_hex, 64)
            or not _is_exact_lower_hex(freeze_sha256, 64)
        ):
            raise _repository_admission_error(
                "cutover_freeze_value_invalid",
            )
        code_digest = bytes.fromhex(code_digest_hex)
        expected_digest = _repository_cutover_freeze_digest(
            authority_epoch=authority_epoch,
            source_admission_revision=source_revision,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
        )
        if freeze_sha256 != expected_digest:
            raise _repository_admission_error(
                "cutover_freeze_binding_invalid",
            )
        return _RepositoryCutoverFreeze(
            authority_epoch=authority_epoch,
            source_admission_revision=source_revision,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
            freeze_sha256=freeze_sha256,
        )

    @staticmethod
    def _source_revision_for_cutover_admission(
        admission: _RepositoryAdmission,
    ) -> int:
        offsets = {"legacy": 0, "cutover_pending": 1, "sqlite": 2}
        try:
            source_revision = admission.revision - offsets[admission.authority_state]
        except (AttributeError, KeyError, TypeError):
            raise _repository_admission_error(
                "cutover_freeze_admission_invalid",
            ) from None
        if not 1 <= source_revision <= _MAX_PROJECT_GENERATION - 2:
            raise _repository_admission_error(
                "cutover_freeze_admission_revision_invalid",
            )
        return source_revision

    def _require_repository_cutover_freeze(
        self,
        admission: _RepositoryAdmission,
    ) -> _RepositoryCutoverFreeze:
        marker = self._read_repository_cutover_freeze()
        if marker is None:
            raise _repository_admission_error("cutover_freeze_missing")
        source_revision = self._source_revision_for_cutover_admission(admission)
        if (
            marker.authority_epoch != admission.authority_epoch
            or marker.source_admission_revision != source_revision
        ):
            raise _repository_admission_error(
                "cutover_freeze_admission_mismatch",
            )
        return marker

    def _install_repository_cutover_freeze(
        self,
        expected: _RepositoryAdmission,
        *,
        runtime_generation: str,
        code_digest: bytes,
        _capability: Any = None,
    ) -> _RepositoryCutoverFreeze:
        """Install the irreversible source fence inside the strict EX lease."""

        if _capability is not _REPOSITORY_CUTOVER_FREEZE_CAPABILITY:
            raise ZeError(
                "project_topology_forbidden",
                "repository cutover freeze capability is required",
                context={},
            )
        if (
            type(expected) is not _RepositoryAdmission
            or expected.authority_state != "legacy"
            or expected.cutover_id != ""
            or expected.revision > _MAX_PROJECT_GENERATION - 2
        ):
            raise ZeError(
                "project_topology_conflict",
                "repository cutover freeze requires exact legacy admission",
                context={},
            )
        if type(runtime_generation) is not str:
            raise TypeError("runtime_generation must be an exact string")
        try:
            generation_bytes = runtime_generation.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise ValueError("runtime_generation must be valid UTF-8") from exc
        if not 1 <= len(generation_bytes) <= (
            _REPOSITORY_CUTOVER_RUNTIME_GENERATION_MAX_BYTES
        ):
            raise ValueError("runtime_generation exceeds the cutover budget")
        if type(code_digest) is not bytes or len(code_digest) != 32:
            raise TypeError("code_digest must be exactly 32 bytes")

        if not self._strict_repository_ex_lease_held():
            raise ZeError(
                "project_topology_forbidden",
                "strict repository migration outer lease is required",
                context={},
            )

        current = self._read_repository_admission()
        assert current is not None
        if current != expected:
            raise ZeError(
                "project_topology_conflict",
                "repository admission changed before source freeze",
                context={
                    "authority_state": current.authority_state,
                    "revision": current.revision,
                },
            )
        # Install only after proving one exact terminal source.  Once the
        # marker exists every ordinary SH lease is denied, so this projection
        # remains stable even if the installing process dies immediately.
        self._strict_repository_snapshot_locked(current)
        desired = _RepositoryCutoverFreeze(
            authority_epoch=current.authority_epoch,
            source_admission_revision=current.revision,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
            freeze_sha256=_repository_cutover_freeze_digest(
                authority_epoch=current.authority_epoch,
                source_admission_revision=current.revision,
                runtime_generation=runtime_generation,
                code_digest=code_digest,
            ),
        )
        observed = self._read_repository_cutover_freeze()
        if observed is not None:
            if observed == desired:
                return observed
            raise ZeError(
                "project_topology_conflict",
                "repository source is frozen by another deployment identity",
                context={"reason": "cutover_freeze_identity_conflict"},
            )
        try:
            _atomic_json(
                self._repository_cutover_freeze_path,
                _cutover_freeze_value(desired),
                mode=0o600,
            )
        except BaseException as exc:
            observed = self._read_repository_cutover_freeze()
            if observed == desired:
                if not isinstance(exc, Exception):
                    raise
                raise ZeError(
                    "project_topology_durability_unknown",
                    "repository source freeze may already be durable",
                    context={"source_admission_revision": current.revision},
                ) from exc
            raise
        observed = self._read_repository_cutover_freeze()
        if observed != desired:
            raise _repository_admission_error(
                "cutover_freeze_write_mismatch",
            )
        return observed

    def _finish_repository_admission_install_locked(
        self,
        marker: _RepositoryAdmissionInstallMarker,
        existing: _RepositoryAdmission | None,
        *,
        bootstrap_validate: Validate | None = None,
    ) -> _RepositoryAdmission:
        if marker.phase != "prepared":
            raise _repository_admission_error(
                "admission_install_marker_phase_invalid",
            )
        initial = self._initial_admission_for_install_marker(marker)
        if existing is not None and existing != initial:
            raise _repository_admission_error(
                "admission_install_prepared_record_mismatch",
            )
        if marker.install_mode == "legacy_bootstrap" and bootstrap_validate is None:
            raise _repository_admission_error(
                "admission_legacy_bootstrap_resume_required",
            )
        if marker.install_mode == "legacy_bootstrap":
            # initialize_locked enumerates and may remove children from these
            # controls.  Prove both named paths are owner-controlled real
            # directories before writing a missing admission record or
            # touching a single child.
            self._validate_repository_namespace_controls()
        if existing is None:
            # The exact initial record is a temporary legacy writer fence, but
            # no constructor has returned and barrier EX + repo EX are held.
            # It must exist before initialize() can enter its ordinary lease.
            self._write_repository_admission(initial)
        if marker.install_mode == "legacy_bootstrap":
            assert bootstrap_validate is not None
            # The initial admission exists but no constructor has returned;
            # barrier EX + repo EX remain held.  Reuse the exact v4 recovery
            # path before proving closure, so a recoverable journal/staging/
            # trash edge cannot deadlock the version rollout.
            self._initialize_locked(bootstrap_validate)
            self._strict_catalog_snapshot_locked()
        committed = _RepositoryAdmissionInstallMarker(
            phase="committed",
            install_mode=marker.install_mode,
            authority_epoch=marker.authority_epoch,
            initial_record_sha256=marker.initial_record_sha256,
        )
        self._write_repository_admission_install_marker(committed)
        return initial

    def _install_repository_admission_locked(
        self,
        initial: _RepositoryAdmission,
        *,
        install_mode: str,
        bootstrap_validate: Validate | None = None,
    ) -> None:
        prepared = _RepositoryAdmissionInstallMarker(
            phase="prepared",
            install_mode=install_mode,
            authority_epoch=initial.authority_epoch,
            initial_record_sha256=_repository_admission_digest(initial),
        )
        self._write_repository_admission_install_marker(prepared)
        self._write_repository_admission(initial)
        self._finish_repository_admission_install_locked(
            prepared,
            initial,
            bootstrap_validate=bootstrap_validate,
        )

    def _initialize_repository_admission(
        self,
        *,
        control_preexisting: bool,
        bootstrap_capability: Any,
        bootstrap_validate: Validate | None,
    ) -> None:
        existing = self._read_repository_admission(allow_missing=True)
        marker = self._read_repository_admission_install_marker()
        if marker is not None and marker.phase == "committed":
            if existing is None:
                raise _repository_admission_error("admission_missing")
            if existing.authority_epoch != marker.authority_epoch:
                raise _repository_admission_error(
                    "admission_install_committed_epoch_mismatch",
                )
            if bootstrap_capability is _REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY:
                initial = self._initial_admission_for_install_marker(marker)
                if marker.install_mode == "legacy_bootstrap" and existing == initial:
                    # Concurrent rollout workers may arrive after the exact
                    # trusted install has already committed.  Reconcile that
                    # single immutable initial outcome; never treat a later
                    # revision, rollback, pending, or sqlite state as bootstrap.
                    return
                raise ZeError(
                    "project_topology_conflict",
                    "repository admission is already installed",
                    context={
                        "authority_state": existing.authority_state,
                        "revision": existing.revision,
                    },
                )
            return
        if marker is None and existing is not None:
            raise _repository_admission_error(
                "admission_install_marker_missing",
            )
        if marker is None and (
            control_preexisting
            and bootstrap_capability
            is not _REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY
        ):
            raise _repository_admission_error("admission_missing")
        try:
            with _path_lock(
                self.catalog_migration_lock,
                exclusive=True,
                timeout=self.lock_timeout,
            ):
                # No ordinary coordinator can pass admission while it is
                # absent, so barrier EX alone serializes first installation.
                # Deliberately avoid creating repo.lock during construction;
                # initialize() remains the first ordinary repository lease.
                current = self._read_repository_admission(allow_missing=True)
                current_marker = self._read_repository_admission_install_marker()
                if current_marker is not None:
                    if current_marker.phase == "committed":
                        if current is None:
                            raise _repository_admission_error("admission_missing")
                        if current.authority_epoch != current_marker.authority_epoch:
                            raise _repository_admission_error(
                                "admission_install_committed_epoch_mismatch",
                            )
                        if (
                            bootstrap_capability
                            is _REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY
                        ):
                            initial = self._initial_admission_for_install_marker(
                                current_marker,
                            )
                            if not (
                                current_marker.install_mode == "legacy_bootstrap"
                                and current == initial
                            ):
                                raise ZeError(
                                    "project_topology_conflict",
                                    "repository admission is already installed",
                                    context={
                                        "authority_state": current.authority_state,
                                        "revision": current.revision,
                                    },
                                )
                        return
                    with _path_lock(
                        self.repo_lock,
                        exclusive=True,
                        timeout=self.lock_timeout,
                    ):
                        self._finish_repository_admission_install_locked(
                            current_marker,
                            current,
                            bootstrap_validate=bootstrap_validate,
                        )
                        return
                if current is not None:
                    raise _repository_admission_error(
                        "admission_install_marker_missing",
                    )
                initial = _RepositoryAdmission(
                    authority_state="legacy",
                    authority_epoch=os.urandom(32).hex(),
                    revision=1,
                    cutover_id="",
                )
                if control_preexisting:
                    # Existing v4 repositories may install v5 admission only
                    # through this private stop-the-world seam and only when
                    # their complete terminal catalog closes.
                    with _path_lock(
                        self.repo_lock,
                        exclusive=True,
                        timeout=self.lock_timeout,
                    ):
                        self._validate_repository_namespace_controls()
                        self._install_repository_admission_locked(
                            initial,
                            install_mode="legacy_bootstrap",
                            bootstrap_validate=bootstrap_validate,
                        )
                        return
                self._install_repository_admission_locked(
                    initial,
                    install_mode="fresh",
                )
        except _PathLockTimeout as exc:
            raise ZeError(
                "project_busy",
                "repository admission initialization timed out",
                context={},
            ) from exc

    @classmethod
    def _bootstrap_existing_legacy_repository_admission(
        cls,
        projects_root: Path,
        *,
        durable_entries: tuple[str, ...] = DEFAULT_DURABLE_ENTRIES,
        lock_timeout: float = 10.0,
        validate: Validate,
        _capability: Any = None,
    ) -> "ProjectTopologyCoordinator":
        """Install v5 writer admission into one proven closed v4 repository."""

        if _capability is not _REPOSITORY_ADMISSION_BOOTSTRAP_CAPABILITY:
            raise ZeError(
                "project_topology_forbidden",
                "repository admission bootstrap capability is required",
                context={},
            )
        return cls(
            projects_root,
            durable_entries=durable_entries,
            lock_timeout=lock_timeout,
            _repository_admission_bootstrap_capability=_capability,
            _repository_admission_bootstrap_validate=validate,
        )

    def _strict_repository_ex_lease_held(self) -> bool:
        if getattr(_STRICT_REPOSITORY_MIGRATION_SCOPE, "depth", 0) < 1:
            return False
        held = getattr(_HELD_LOCKS, "entries", None)
        for lock_path in (self.catalog_migration_lock, self.repo_lock):
            entry = (
                held.get(str(lock_path.absolute()))
                if type(held) is dict
                else None
            )
            if not (
                type(entry) is dict
                and entry.get("exclusive") is True
                and type(entry.get("depth")) is int
                and entry["depth"] >= 1
                and entry.get("pid") == os.getpid()
            ):
                return False
        return True

    def _require_legacy_repository_admission(self) -> _RepositoryAdmission:
        admission = self._read_repository_admission()
        assert admission is not None
        if admission.authority_state == "legacy":
            marker = self._read_repository_cutover_freeze()
            if marker is None:
                return admission
            self._require_repository_cutover_freeze(admission)
            if self._strict_repository_ex_lease_held():
                return admission
            raise ZeError(
                "project_authority_moved",
                "legacy file repository is durably frozen for cutover",
                context={
                    "authority_state": "legacy_frozen",
                    "authority_epoch": admission.authority_epoch,
                    "revision": admission.revision,
                },
            )
        # Pending/final admission without the predecessor freeze is not a
        # legitimate authority move.  Diagnose it as broken cross-file state
        # before returning the ordinary path-level fence.
        self._require_repository_cutover_freeze(admission)
        if self._strict_repository_ex_lease_held():
            return admission
        raise ZeError(
            "project_authority_moved",
            "legacy file repository writer is fenced",
            context={
                "authority_state": admission.authority_state,
                "authority_epoch": admission.authority_epoch,
                "revision": admission.revision,
            },
        )

    # ------------------------------------------------------------------
    # Paths, state, and generation
    # ------------------------------------------------------------------

    def path_for(self, project_id: str) -> Path:
        pid = self._validate_project_id(project_id)
        return self.projects_root / pid

    def _validate_project_id(self, project_id: str) -> str:
        bad = (
            not isinstance(project_id, str)
            or not project_id
            or len(project_id) > 160
            or project_id in (".", "..")
            or project_id.startswith(".ze-")
            or any(char in project_id for char in ("/", "\\", "\x00", "\n", "\r", "\t"))
        )
        if bad:
            raise ZeError(
                "project_not_found",
                "invalid project_id",
                context={"project_id": str(project_id)[:160]},
            )
        candidate = (self.projects_root / project_id).absolute()
        if candidate.parent != self.projects_root:
            raise ZeError("project_not_found", "invalid project_id", context={})
        return project_id

    def _project_lock_path(self, project_id: str) -> Path:
        digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
        return self.lock_root / f"{digest}.lock"

    def _state_path(self, project_id: str) -> Path:
        digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
        return self.state_root / f"{digest}.json"

    def _journal_path(self, operation_id: str) -> Path:
        return self.journal_root / f"{operation_id}.json"

    def _outcome_path(self, operation_id: str) -> Path:
        return self.outcome_root / f"{operation_id}.json"

    def _intent_path(self, operation_id: str) -> Path:
        return self.intent_root / f"{operation_id}.json"

    @staticmethod
    def _validate_operation_id(operation_id: str) -> str:
        if (
            not isinstance(operation_id, str)
            or len(operation_id) != 32
            or any(char not in "0123456789abcdef" for char in operation_id)
        ):
            raise ZeError(
                "validation_failed",
                "operation_id must be 32 lowercase hex characters",
            )
        return operation_id

    @staticmethod
    def _legacy_adoption_operation_id(project_id: str) -> str:
        """Return one deterministic canonical id for pre-journal live roots."""
        return hashlib.sha256(
            b"ze.project-topology.legacy-adopt.v1\0"
            + project_id.encode("utf-8")
        ).hexdigest()[:32]

    @staticmethod
    def _adopt_heal_operation_id(project_id: str, generation: int) -> str:
        """Deterministic id marking a startup self-healed adoption in the catalog."""
        return hashlib.sha256(
            b"ze.project-topology.adopt-heal.v1\0"
            + f"{project_id}\0{generation}".encode("utf-8")
        ).hexdigest()[:32]

    def _adopt_heal(
        self, root: Path, pid: str, generation: int, validate: Validate,
    ) -> None:
        """启动对账时就地重建一个静默项目的身份记录(marker+状态行),失败保持冻结。

        只会从 _initialize_locked 到达:journal 恢复已完成、持 repo EX + 项目锁,目录必然
        静默。此时权威事实是「这个路径(=项目 id)下有一份合法且命名空间相符的 Document」;
        marker/状态行只是派生簿记,可确定性再生。validate + _write_marker(内含 Document
        身份守卫:durable id 必须等于命名空间)任何一步不过 → 维持 fail-closed 冻结,状态行
        的 operation_id 留下可读原因供 _require_live 上报。"""
        try:
            validate(root)
            heal_id = self._adopt_heal_operation_id(pid, generation)
            self._write_marker(root, pid, generation, heal_id)
            _fsync_tree(root)
            self._write_state(pid, generation, "live", heal_id)
        except ZeError as exc:
            if exc.code != "project_recovery_required":
                raise
            self._freeze(pid, generation, "adopt-invalid-document")

    def _marker_path(self, root: Path) -> Path:
        return root / PROJECT_MARKER

    def _read_marker(self, root: Path) -> dict[str, Any] | None:
        if not root.exists() and not root.is_symlink():
            return None
        try:
            root_info = root.lstat()
        except OSError as exc:
            raise ZeError(
                "project_recovery_required", "project root cannot be inspected",
                context={"entry": root.name},
            ) from exc
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise ZeError(
                "project_recovery_required",
                "project root must be a real directory",
                context={"entry": root.name},
            )
        marker = _read_json(self._marker_path(root))
        if marker is None:
            return None
        if (
            marker.get("schema") != MARKER_SCHEMA
            or not isinstance(marker.get("project_id"), str)
            or not isinstance(marker.get("generation"), int)
            or isinstance(marker.get("generation"), bool)
            or marker["generation"] < 1
            or not isinstance(marker.get("operation_id"), str)
        ):
            raise ZeError(
                "project_recovery_required",
                "project generation marker is invalid",
                context={"entry": PROJECT_MARKER},
            )
        return marker

    @staticmethod
    def _marker_matches(
        marker: dict[str, Any] | None,
        *,
        project_id: str,
        generation: int,
        operation_id: str | None = None,
    ) -> bool:
        return bool(
            marker
            and marker.get("project_id") == project_id
            and marker.get("generation") == generation
            and (operation_id is None or marker.get("operation_id") == operation_id)
        )

    def _write_marker(self, root: Path, project_id: str, generation: int, operation_id: str) -> None:
        # A topology marker is an identity assertion, not merely a directory
        # label.  Never create one for a Document whose durable id names a
        # different namespace.
        with self._document_identity_guard(root, project_id):
            _atomic_json(self._marker_path(root), {
                "schema": MARKER_SCHEMA,
                "project_id": project_id,
                "generation": generation,
                "operation_id": operation_id,
            })

    @staticmethod
    def _assert_head_identity(head: DocumentHead, project_id: str) -> None:
        if head.metadata.id != project_id:
            raise ZeError(
                "project_recovery_required",
                "project namespace and document identity disagree",
                context={
                    "project_id": project_id,
                    "reason": "document_id_mismatch",
                },
            )

    @contextmanager
    def _document_identity_guard(
        self,
        root: Path,
        project_id: str,
    ) -> Iterator[DocumentHead]:
        """Hold one validated head while proving its namespace identity."""
        with open_document_head(root / "document.json") as head:
            assert head is not None
            self._assert_head_identity(head, project_id)
            yield head

    def _read_state(self, project_id: str) -> dict[str, Any] | None:
        state = _read_json(self._state_path(project_id))
        if state is None:
            return None
        if (
            state.get("schema") != STATE_SCHEMA
            or state.get("project_id") != project_id
            or not isinstance(state.get("generation"), int)
            or isinstance(state.get("generation"), bool)
            or state["generation"] < 1
            or state.get("status") not in (
                "live", "quiescing", "deleted", "recovery_required",
            )
        ):
            raise ZeError(
                "project_recovery_required",
                "project catalog state is invalid",
                context={"project_id": project_id},
            )
        return state

    def _write_state(
        self,
        project_id: str,
        generation: int,
        status: str,
        operation_id: str,
    ) -> None:
        _atomic_json(self._state_path(project_id), {
            "schema": STATE_SCHEMA,
            "project_id": project_id,
            "generation": generation,
            "status": status,
            "operation_id": operation_id,
        })

    def _write_journal(self, journal: dict[str, Any]) -> None:
        _atomic_json(self._journal_path(journal["operation_id"]), journal)

    @staticmethod
    def _principal_hash(principal_id: str) -> str:
        if not isinstance(principal_id, str) or len(principal_id) > 512:
            raise ZeError("validation_failed", "invalid topology principal")
        if not principal_id:
            return ""
        return hashlib.sha256(
            b"ze.project-topology.principal.v1\0" + principal_id.encode("utf-8")
        ).hexdigest()

    def _read_principal_claim(self, operation_id: str) -> dict[str, Any] | None:
        claim = _read_json(self._intent_path(operation_id))
        if claim is None:
            return None
        principal_hash = claim.get("principal_hash")
        if (
            claim.get("schema") != PRINCIPAL_CLAIM_SCHEMA
            or claim.get("operation_id") != operation_id
            or not isinstance(principal_hash, str)
            or len(principal_hash) != 64
            or any(char not in "0123456789abcdef" for char in principal_hash)
            or not isinstance(claim.get("target_id", ""), str)
            or not isinstance(claim.get("kind", ""), str)
        ):
            raise ZeError(
                "project_recovery_required",
                "project topology principal claim is invalid",
                context={"operation_id": operation_id},
            )
        return claim

    def _claim_principal(
        self,
        operation_id: str,
        principal_hash: str,
        *,
        target_id: str = "",
        kind: str = "",
    ) -> None:
        existing = self._read_principal_claim(operation_id)
        if existing is not None:
            if not principal_hash or existing["principal_hash"] != principal_hash:
                raise ZeError(
                    "acl_denied", "topology intent belongs to another principal",
                    context={"operation_id": operation_id},
                )
            if (
                (target_id and existing.get("target_id") != target_id)
                or (kind and existing.get("kind") != kind)
            ):
                raise ZeError(
                    "project_topology_conflict",
                    "topology intent identity cannot be changed",
                    context={"operation_id": operation_id, "project_id": target_id},
                )
            return
        if not principal_hash:
            return
        _atomic_json(self._intent_path(operation_id), {
            "schema": PRINCIPAL_CLAIM_SCHEMA,
            "operation_id": operation_id,
            "principal_hash": principal_hash,
            "target_id": target_id,
            "kind": kind,
        })

    @staticmethod
    def _outcome_identity(value: dict[str, Any]) -> tuple[str, str, str, str, str]:
        return (
            str(value.get("operation_id") or ""),
            str(value.get("kind") or ""),
            str(value.get("target_id") or ""),
            str(value.get("source_id") or ""),
            str(value.get("principal_hash") or ""),
        )

    def _read_outcome(self, operation_id: str) -> dict[str, Any] | None:
        outcome = _read_json(self._outcome_path(operation_id))
        if outcome is None:
            return None
        if (
            outcome.get("schema") != OUTCOME_SCHEMA
            or outcome.get("operation_id") != operation_id
            or outcome.get("kind") not in TOPOLOGY_KINDS
            or not isinstance(outcome.get("target_id"), str)
            or not isinstance(outcome.get("source_id"), str)
            or outcome.get("outcome") != "committed"
            or not isinstance(outcome.get("generation"), int)
            or isinstance(outcome.get("generation"), bool)
            or outcome["generation"] < 1
            or not isinstance(outcome.get("principal_hash", ""), str)
            or (
                bool(outcome.get("principal_hash"))
                and (
                    len(outcome["principal_hash"]) != 64
                    or any(char not in "0123456789abcdef" for char in outcome["principal_hash"])
                )
            )
        ):
            raise ZeError(
                "project_recovery_required",
                "project topology outcome receipt is invalid",
                context={"operation_id": operation_id},
            )
        self._validate_project_id(outcome["target_id"])
        if outcome["source_id"]:
            self._validate_project_id(outcome["source_id"])
        return outcome

    def _write_committed_outcome(self, journal: dict[str, Any]) -> None:
        """Persist the immutable logical outcome before journal evidence is removed."""
        receipt = {
            "schema": OUTCOME_SCHEMA,
            "operation_id": journal["operation_id"],
            "kind": journal["kind"],
            "source_id": str(journal.get("source_id") or ""),
            "target_id": journal["target_id"],
            "outcome": "committed",
            "generation": int(journal["new_generation"]),
        }
        principal_hash = str(journal.get("principal_hash") or "")
        if principal_hash:
            receipt["principal_hash"] = principal_hash
        existing = self._read_outcome(journal["operation_id"])
        if existing is not None:
            if existing != receipt:
                raise ZeError(
                    "project_recovery_required",
                    "operation_id has contradictory committed outcomes",
                    context={"operation_id": journal["operation_id"]},
                )
            return
        _atomic_json(self._outcome_path(journal["operation_id"]), receipt)

    def _claim_journal(self, journal: dict[str, Any]) -> None:
        """Create initial evidence without ever overwriting another operation."""
        self._claim_principal(
            journal["operation_id"], str(journal.get("principal_hash") or ""),
            target_id=str(journal.get("target_id") or ""),
            kind=str(journal.get("kind") or ""),
        )
        outcome = self._read_outcome(journal["operation_id"])
        if outcome is not None:
            raise ZeError(
                "project_topology_conflict",
                "operation_id already has a committed topology outcome",
                context={
                    "project_id": journal.get("target_id", ""),
                    "operation_id": journal["operation_id"],
                },
            )
        path = self._journal_path(journal["operation_id"])
        if path.exists() or path.is_symlink():
            raise ZeError(
                "project_busy",
                "operation_id already owns unfinished topology evidence",
                context={
                    "project_id": journal.get("target_id", ""),
                    "operation_id": journal["operation_id"],
                },
            )
        self._write_journal(journal)

    def _finish_journal(self, operation_id: str) -> None:
        journal = _read_json(self._journal_path(operation_id))
        if journal is not None and journal.get("phase") in ("committed", "cleanup_pending"):
            self._write_committed_outcome(journal)
        _unlink_durable(self._journal_path(operation_id))

    def _defer_cleanup(
        self,
        journal: dict[str, Any],
        *,
        mark_cleanup_pending: bool = True,
    ) -> None:
        """Keep committed/recoverable evidence without turning cleanup into failure."""
        if mark_cleanup_pending:
            journal["phase"] = "cleanup_pending"
        try:
            self._write_journal(journal)
        except Exception:
            # The previous durable phase remains enough for the next recovery
            # pass.  Never freeze a proven logical commit because bookkeeping
            # cleanup itself could not be acknowledged.
            pass

    def _finish_recovery_cleanup(
        self,
        journal: dict[str, Any],
        *,
        paths: tuple[Path, ...] = (),
        cleanup_sources: bool = False,
        mark_cleanup_pending: bool = True,
    ) -> bool:
        try:
            for path in paths:
                if path.exists() or path.is_symlink():
                    _remove_tree_durable(path)
            if cleanup_sources:
                self._cleanup_sources(journal)
            if journal.get("phase") in ("committed", "cleanup_pending"):
                self._write_committed_outcome(journal)
            self._finish_journal(journal["operation_id"])
        except Exception:
            self._defer_cleanup(
                journal, mark_cleanup_pending=mark_cleanup_pending,
            )
            return False
        return True

    def _assert_no_pending_target(self, project_id: str) -> None:
        """One target may have at most one active topology transition."""
        for journal_path in self.journal_root.glob("*.json"):
            journal = _read_json(journal_path)
            if journal and journal.get("target_id") == project_id:
                raise ZeError(
                    "project_busy",
                    "project has an unfinished topology operation",
                    context={
                        "project_id": project_id,
                        "operation_id": str(journal.get("operation_id") or ""),
                    },
                )

    def _require_live(self, project_id: str, *, expected_generation: int | None = None) -> ProjectHandle:
        if project_id in self._recovery_required:
            frozen = self._read_state(project_id) or {}
            raise ZeError(
                "project_recovery_required",
                "project topology requires repair",
                context={"project_id": project_id,
                         "reason": str(frozen.get("operation_id") or "")},
            )
        state = self._read_state(project_id)
        if state is None or state["status"] == "deleted":
            raise ZeError(
                "project_not_found",
                f"project {project_id!r} not found",
                context={"project_id": project_id},
            )
        if state["status"] == "recovery_required":
            raise ZeError(
                "project_recovery_required",
                "project topology requires repair",
                context={"project_id": project_id,
                         "reason": str(state.get("operation_id") or "")},
            )
        if state["status"] != "live":
            raise ZeError(
                "project_busy",
                "project lifecycle transition is in progress",
                context={"project_id": project_id},
            )
        root = self.path_for(project_id)
        marker = self._read_marker(root)
        if (
            not self._marker_matches(
                marker,
                project_id=project_id,
                generation=state["generation"],
                operation_id=str(state.get("operation_id") or ""),
            )
            or not _is_regular_file(root / "document.json")
        ):
            raise ZeError(
                "project_recovery_required",
                "project catalog and live generation disagree",
                context={"project_id": project_id},
            )
        if expected_generation is not None and marker["generation"] != expected_generation:
            raise ZeError(
                "project_generation_stale",
                "project context belongs to an obsolete generation",
                context={
                    "project_id": project_id,
                    "expected_generation": expected_generation,
                    "current_generation": marker["generation"],
                },
            )
        return ProjectHandle(project_id, root, marker["generation"])

    def _freeze(self, project_id: str, generation: int, operation_id: str) -> None:
        self._recovery_required.add(project_id)
        try:
            self._write_state(project_id, max(1, generation), "recovery_required", operation_id)
        except Exception:
            pass

    @staticmethod
    def _strict_catalog_failure(reason: str, *, entry: str = "") -> None:
        context = {"reason": reason}
        if entry:
            context["entry"] = entry
        raise ZeError(
            "project_recovery_required",
            "project catalog is not closed for migration",
            context=context,
        )

    def _strict_catalog_snapshot_locked(self) -> tuple[ProjectHandle, ...]:
        """Prove one closed topology catalog while repo EX is already held."""

        for control in (
            self.projects_root,
            self.state_root,
            self.journal_root,
            self.staging_root,
            self.trash_root,
        ):
            try:
                info = control.lstat()
            except OSError:
                self._strict_catalog_failure(
                    "catalog_control_missing", entry=control.name,
                )
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                self._strict_catalog_failure(
                    "catalog_control_unsafe", entry=control.name,
                )

        if self._recovery_required:
            self._strict_catalog_failure("catalog_recovery_pending")

        try:
            journal_entries = tuple(self.journal_root.iterdir())
            staging_entries = tuple(self.staging_root.iterdir())
            trash_entries = tuple(self.trash_root.iterdir())
        except OSError:
            self._strict_catalog_failure("catalog_control_unreadable")
        if journal_entries:
            self._strict_catalog_failure(
                "catalog_journal_pending", entry=journal_entries[0].name,
            )
        if staging_entries:
            self._strict_catalog_failure(
                "catalog_staging_not_empty", entry=staging_entries[0].name,
            )
        if trash_entries:
            self._strict_catalog_failure(
                "catalog_trash_not_empty", entry=trash_entries[0].name,
            )

        states: dict[str, dict[str, Any]] = {}
        try:
            state_entries = tuple(self.state_root.iterdir())
        except OSError:
            self._strict_catalog_failure("catalog_state_unreadable")
        for state_path in state_entries:
            try:
                info = state_path.lstat()
            except OSError:
                self._strict_catalog_failure(
                    "catalog_state_unreadable", entry=state_path.name,
                )
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or state_path.suffix != ".json"
            ):
                self._strict_catalog_failure(
                    "catalog_state_entry_invalid", entry=state_path.name,
                )
            raw = _read_json(state_path)
            if raw is None or set(raw) != {
                "schema", "project_id", "generation", "status", "operation_id",
            }:
                self._strict_catalog_failure(
                    "catalog_state_shape_invalid", entry=state_path.name,
                )
            project_id = raw.get("project_id")
            if not isinstance(project_id, str):
                self._strict_catalog_failure(
                    "catalog_state_identity_invalid", entry=state_path.name,
                )
            try:
                project_id = self._validate_project_id(project_id)
            except ZeError:
                self._strict_catalog_failure(
                    "catalog_state_identity_invalid", entry=state_path.name,
                )
            if state_path != self._state_path(project_id):
                self._strict_catalog_failure(
                    "catalog_state_path_mismatch", entry=state_path.name,
                )
            if project_id in states:
                self._strict_catalog_failure(
                    "catalog_state_duplicate", entry=state_path.name,
                )
            generation = raw.get("generation")
            operation_id = raw.get("operation_id")
            status = raw.get("status")
            legacy_adoption = (
                operation_id == "legacy-adopt"
                and status == "live"
                and generation == 1
            )
            if not legacy_adoption:
                try:
                    self._validate_operation_id(operation_id)
                except ZeError:
                    self._strict_catalog_failure(
                        "catalog_state_operation_invalid", entry=state_path.name,
                    )
            if (
                raw.get("schema") != STATE_SCHEMA
                or type(generation) is not int
                or not 1 <= generation <= _MAX_PROJECT_GENERATION
                or status not in ("live", "deleted")
            ):
                self._strict_catalog_failure(
                    "catalog_state_not_terminal", entry=state_path.name,
                )
            states[project_id] = raw

        live_states = {
            project_id: state
            for project_id, state in states.items()
            if state["status"] == "live"
        }
        roots: dict[str, Path] = {}
        try:
            root_entries = tuple(self.projects_root.iterdir())
        except OSError:
            self._strict_catalog_failure("catalog_namespace_unreadable")
        for root in root_entries:
            if root.name in CONTROL_NAMES:
                continue
            try:
                info = root.lstat()
            except OSError:
                self._strict_catalog_failure(
                    "catalog_root_unreadable", entry=root.name,
                )
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                self._strict_catalog_failure(
                    "catalog_root_unsafe", entry=root.name,
                )
            try:
                project_id = self._validate_project_id(root.name)
            except ZeError:
                self._strict_catalog_failure(
                    "catalog_root_identity_invalid", entry=root.name,
                )
            if project_id in roots or project_id not in live_states:
                self._strict_catalog_failure(
                    "catalog_orphan_root", entry=root.name,
                )
            roots[project_id] = root

        missing_roots = sorted(set(live_states).difference(roots))
        if missing_roots:
            self._strict_catalog_failure(
                "catalog_live_root_missing", entry=missing_roots[0],
            )

        handles: list[ProjectHandle] = []
        for project_id in sorted(live_states):
            state = live_states[project_id]
            root = roots[project_id]
            marker_path = self._marker_path(root)
            if not _is_regular_file(marker_path):
                self._strict_catalog_failure(
                    "catalog_marker_invalid", entry=project_id,
                )
            marker = self._read_marker(root)
            if (
                marker is None
                or set(marker) != {
                    "schema", "project_id", "generation", "operation_id",
                }
                or marker.get("schema") != MARKER_SCHEMA
                or marker.get("project_id") != project_id
                or marker.get("generation") != state["generation"]
                or marker.get("operation_id") != state["operation_id"]
            ):
                self._strict_catalog_failure(
                    "catalog_marker_mismatch", entry=project_id,
                )
            handle = self._require_live(
                project_id,
                expected_generation=state["generation"],
            )
            with self._document_identity_guard(root, project_id):
                pass
            handles.append(handle)
        return tuple(handles)

    def _strict_repository_snapshot_locked(
        self,
        admission: _RepositoryAdmission,
    ) -> _StrictRepositorySnapshot:
        """Extend the strict live catalog with exact terminal tombstones."""

        live_handles = self._strict_catalog_snapshot_locked()
        live_by_id = {handle.project_id: handle for handle in live_handles}
        descriptors: list[_RepositoryStateDescriptor] = []
        for state_path in sorted(self.state_root.iterdir(), key=lambda item: item.name):
            raw = _read_json(state_path)
            # The strict catalog proof immediately above has already rejected
            # malformed, extra, misplaced, nonterminal and duplicate entries.
            # Re-read only to construct immutable values; repo EX prevents a
            # supported writer from changing them between the two operations.
            if raw is None:
                self._strict_catalog_failure(
                    "catalog_state_unreadable", entry=state_path.name,
                )
            project_id = str(raw["project_id"])
            status = str(raw["status"])
            if status == "live":
                handle = live_by_id[project_id]
                # Consume the validated head authority directly.  Its payload
                # is the exact immutable byte sequence parsed under the content
                # lock; a second raw read would create a competing head reader.
                with self._document_identity_guard(
                    handle.path, project_id,
                ) as head:
                    source_bytes = bytes(head.payload)
            else:
                source_bytes = b""
            descriptors.append(_RepositoryStateDescriptor(
                project_id=project_id,
                generation=int(raw["generation"]),
                status=status,
                operation_id=str(raw["operation_id"]),
                source_bytes=source_bytes,
                source_sha256=hashlib.sha256(source_bytes).digest(),
            ))
        descriptors.sort(key=lambda item: item.project_id)
        return _StrictRepositorySnapshot(
            admission=admission,
            states=tuple(descriptors),
        )

    # ------------------------------------------------------------------
    # Runtime leases
    # ------------------------------------------------------------------

    @contextmanager
    def _open_repository_lock(self, *, exclusive: bool) -> Iterator[None]:
        """Acquire every ordinary repo lease in barrier -> repo order."""

        with _path_lock(
            self.catalog_migration_lock,
            exclusive=False,
            timeout=self.lock_timeout,
        ):
            # The admission record is stable while barrier SH is held: its
            # sole mutator requires barrier EX.  Reject before repo acquisition
            # and therefore before any legacy business read or write.
            self._require_legacy_repository_admission()
            with _path_lock(
                self.repo_lock,
                exclusive=exclusive,
                timeout=self.lock_timeout,
            ):
                yield

    @contextmanager
    def _open_sqlite_repository_authority(
        self,
        *,
        repository_epoch: str,
        cutover_id: str,
        source_admission_revision: int,
        runtime_generation: str,
        code_digest: bytes,
        _capability: Any = None,
    ) -> Iterator[_RepositoryAdmission]:
        """Hold the file-side half of one exact SQLite authority lease.

        Production SQLite readers and writers must enter this shared barrier
        before opening a database transaction.  The lease proves that the
        irreversible ``legacy -> cutover_pending -> sqlite`` file fence has
        reached the exact successor bound into the SQLite install receipt.
        It grants no legacy path access and deliberately does not acquire the
        repository lock, preserving the only legal order: file barrier first,
        SQLite transaction second.
        """

        if _capability is not _REPOSITORY_SQLITE_AUTHORITY_CAPABILITY:
            raise ZeError(
                "project_topology_forbidden",
                "SQLite repository authority capability is required",
                context={},
            )
        if not _is_exact_lower_hex(repository_epoch, 64):
            raise ZeError(
                "validation_failed",
                "repository_epoch must be 64 lowercase hex characters",
                context={},
            )
        if not _is_exact_lower_hex(cutover_id, 64):
            raise ZeError(
                "validation_failed",
                "cutover_id must be 64 lowercase hex characters",
                context={},
            )
        if type(runtime_generation) is not str:
            raise TypeError("runtime_generation must be an exact string")
        try:
            generation_bytes = runtime_generation.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise ValueError("runtime_generation must be valid UTF-8") from exc
        if not 1 <= len(generation_bytes) <= (
            _REPOSITORY_CUTOVER_RUNTIME_GENERATION_MAX_BYTES
        ):
            raise ValueError("runtime_generation exceeds the cutover budget")
        if type(code_digest) is not bytes or len(code_digest) != 32:
            raise TypeError("code_digest must be exactly 32 bytes")
        if (
            type(source_admission_revision) is not int
            or not 1 <= source_admission_revision <= _MAX_PROJECT_GENERATION - 2
        ):
            raise ZeError(
                "validation_failed",
                "source admission revision cannot reach the SQLite successor",
                context={},
            )
        expected_final_revision = source_admission_revision + 2
        try:
            with _path_lock(
                self.catalog_migration_lock,
                exclusive=False,
                timeout=self.lock_timeout,
            ):
                admission = self._read_repository_admission()
                assert admission is not None
                if admission.authority_state != "sqlite":
                    raise ZeError(
                        "project_authority_moved",
                        "SQLite repository authority is not installed",
                        context={
                            "authority_state": admission.authority_state,
                            "authority_epoch": admission.authority_epoch,
                            "revision": admission.revision,
                        },
                    )
                if (
                    admission.authority_epoch != repository_epoch
                    or admission.cutover_id != cutover_id
                    or admission.revision != expected_final_revision
                ):
                    raise ZeError(
                        "project_recovery_required",
                        "SQLite repository admission does not match its install receipt",
                        context={"reason": "sqlite_admission_mismatch"},
                    )
                marker = self._require_repository_cutover_freeze(admission)
                if (
                    marker.runtime_generation != runtime_generation
                    or marker.code_digest != code_digest
                ):
                    raise ZeError(
                        "project_recovery_required",
                        "SQLite repository identity differs from its source freeze",
                        context={"reason": "sqlite_cutover_freeze_mismatch"},
                    )
                yield admission
        except _PathLockTimeout as exc:
            raise ZeError(
                "project_busy",
                "SQLite repository authority lease timed out",
                context={},
            ) from exc

    @contextmanager
    def _open_strict_repository_migration(
        self,
        *,
        _capability: Any = None,
    ) -> Iterator[_StrictRepositorySnapshot]:
        """Yield a path-free complete state snapshot under EX -> EX locks."""

        if _capability is not _REPOSITORY_ADMISSION_CAPABILITY:
            raise ZeError(
                "project_topology_forbidden",
                "strict repository migration capability is required",
                context={},
            )
        try:
            with _path_lock(
                self.catalog_migration_lock,
                exclusive=True,
                timeout=self.lock_timeout,
            ):
                with _path_lock(
                    self.repo_lock,
                    exclusive=True,
                    timeout=self.lock_timeout,
                ):
                    previous_scope_depth = getattr(
                        _STRICT_REPOSITORY_MIGRATION_SCOPE,
                        "depth",
                        0,
                    )
                    _STRICT_REPOSITORY_MIGRATION_SCOPE.depth = (
                        previous_scope_depth + 1
                    )
                    try:
                        admission = self._read_repository_admission()
                        assert admission is not None
                        if admission.authority_state == "sqlite":
                            raise ZeError(
                                "project_authority_moved",
                                "legacy repository snapshot is fenced",
                                context={
                                    "authority_state": admission.authority_state,
                                    "authority_epoch": admission.authority_epoch,
                                    "revision": admission.revision,
                                },
                            )
                        snapshot = self._strict_repository_snapshot_locked(
                            admission,
                        )
                        yield snapshot
                    finally:
                        _STRICT_REPOSITORY_MIGRATION_SCOPE.depth = (
                            previous_scope_depth
                        )
        except _PathLockTimeout as exc:
            raise ZeError(
                "project_busy",
                "strict repository migration barrier timed out",
                context={},
            ) from exc

    def _revalidate_strict_repository_snapshot(
        self,
        expected: _StrictRepositorySnapshot,
        *,
        _capability: Any = None,
    ) -> _StrictRepositorySnapshot:
        """Rebuild one exact snapshot only inside the caller's EX -> EX lease.

        This is deliberately not a convenience snapshot API.  The private
        repository sealer uses it at acknowledgement boundaries while the
        outer ``_open_strict_repository_migration`` context is still active.
        Checking the thread-local lock registry prevents a caller from using
        an old capability to perform path-based enumeration after that context
        has exited (or in a forked child whose inherited descriptors were
        closed).  The returned carrier remains path-free and handle-free.
        """

        if _capability is not _REPOSITORY_ADMISSION_CAPABILITY:
            raise ZeError(
                "project_topology_forbidden",
                "strict repository migration capability is required",
                context={},
            )
        if type(expected) is not _StrictRepositorySnapshot:
            raise TypeError("expected must be an exact strict repository snapshot")

        if not self._strict_repository_ex_lease_held():
            raise ZeError(
                "project_topology_forbidden",
                "strict repository migration outer lease is required",
                context={},
            )

        admission = self._read_repository_admission()
        assert admission is not None
        observed = self._strict_repository_snapshot_locked(admission)
        if observed != expected:
            raise ZeError(
                "project_topology_conflict",
                "strict repository snapshot changed during migration",
                context={
                    "authority_state": admission.authority_state,
                    "revision": admission.revision,
                },
            )
        return observed

    @staticmethod
    def _desired_repository_admission_transition(
        expected: _RepositoryAdmission,
        *,
        target_state: str,
        cutover_id: str,
    ) -> _RepositoryAdmission:
        if type(expected) is not _RepositoryAdmission:
            raise TypeError("expected must be an exact repository admission")
        if type(target_state) is not str:
            raise TypeError("target_state must be an exact string")
        if expected.revision >= _MAX_PROJECT_GENERATION:
            raise ZeError(
                "project_topology_conflict",
                "repository admission revision is exhausted",
                context={"revision": expected.revision},
            )
        if not _is_exact_lower_hex(cutover_id, 64):
            raise ZeError(
                "validation_failed",
                "cutover_id must be 64 lowercase hex characters",
                context={},
            )
        if expected.authority_state == "legacy" and target_state == "cutover_pending":
            next_cutover_id = cutover_id
        elif (
            expected.authority_state == "cutover_pending"
            and target_state == "sqlite"
            and cutover_id == expected.cutover_id
        ):
            next_cutover_id = cutover_id
        else:
            raise ZeError(
                "project_topology_conflict",
                "repository admission transition is not allowed",
                context={
                    "authority_state": expected.authority_state,
                    "target_state": target_state,
                },
            )
        return _RepositoryAdmission(
            authority_state=target_state,
            authority_epoch=expected.authority_epoch,
            revision=expected.revision + 1,
            cutover_id=next_cutover_id,
        )

    def _compare_and_swap_repository_admission(
        self,
        expected: _RepositoryAdmission,
        *,
        target_state: str,
        cutover_id: str,
        _capability: Any = None,
    ) -> _RepositoryAdmission:
        """Perform one exact private admission transition under EX -> EX.

        The epoch is the immutable 256-bit repository identity.  Revision is
        the monotonic fencing number and advances exactly once per durable
        state edge.  A retry with the original expected value reconciles the
        exact already-written successor, including a lost return edge.
        """

        if _capability is not _REPOSITORY_ADMISSION_CAPABILITY:
            raise ZeError(
                "project_topology_forbidden",
                "repository admission transition capability is required",
                context={},
            )
        desired = self._desired_repository_admission_transition(
            expected,
            target_state=target_state,
            cutover_id=cutover_id,
        )
        try:
            with _path_lock(
                self.catalog_migration_lock,
                exclusive=True,
                timeout=self.lock_timeout,
            ):
                with _path_lock(
                    self.repo_lock,
                    exclusive=True,
                    timeout=self.lock_timeout,
                ):
                    current = self._read_repository_admission()
                    assert current is not None
                    if current == desired:
                        self._require_repository_cutover_freeze(current)
                        return current
                    if current != expected:
                        raise ZeError(
                            "project_topology_conflict",
                            "repository admission compare-and-swap is stale",
                            context={
                                "authority_state": current.authority_state,
                                "revision": current.revision,
                            },
                        )
                    self._require_repository_cutover_freeze(current)
                    # Every state edge also requires one exact terminal file
                    # catalog.  This is a fence substrate, not authority flip.
                    self._strict_repository_snapshot_locked(current)
                    try:
                        self._write_repository_admission(desired)
                    except BaseException as exc:
                        observed = self._read_repository_admission()
                        if observed == desired:
                            if not isinstance(exc, Exception):
                                raise
                            raise ZeError(
                                "project_topology_durability_unknown",
                                "repository admission may already be durable",
                                context={
                                    "authority_state": desired.authority_state,
                                    "revision": desired.revision,
                                },
                            ) from exc
                        raise
                    return desired
        except _PathLockTimeout as exc:
            raise ZeError(
                "project_busy",
                "repository admission transition timed out",
                context={},
            ) from exc

    @contextmanager
    def open_strict_catalog_migration(self) -> Iterator[tuple[ProjectHandle, ...]]:
        """Hold the repository-wide write barrier around one exact catalog.

        The order is migration barrier EX -> repository EX.  Every ordinary
        repository lease and supported business activity takes the matching
        barrier SH first, so entering this context drains in-flight users and
        blocks new ones without a repo/barrier inversion.  Repository EX then
        prevents create, duplicate, replace, delete, initialization, and
        catalog enumeration from racing the proof or caller migration work.
        """

        try:
            with _path_lock(
                self.catalog_migration_lock,
                exclusive=True,
                timeout=self.lock_timeout,
            ):
                with _path_lock(
                    self.repo_lock,
                    exclusive=True,
                    timeout=self.lock_timeout,
                ):
                    self._require_legacy_repository_admission()
                    handles = self._strict_catalog_snapshot_locked()
                    yield handles
        except _PathLockTimeout as exc:
            raise ZeError(
                "project_busy",
                "project catalog migration barrier timed out",
                context={},
            ) from exc

    @contextmanager
    def legacy_migration_guard(self) -> Iterator[None]:
        """Serialize root-layout discovery before acquiring its content lock.

        The order is always migration barrier SH -> repository EX -> legacy
        document EX.  ``create`` re-enters both repository locks in the owning
        thread, so concurrent bootstraps cannot both discover one legacy head
        and no content->repo lock inversion is introduced.
        """
        try:
            with self._open_repository_lock(exclusive=True):
                yield
        except _PathLockTimeout as exc:
            raise ZeError(
                "project_busy", "legacy project migration is busy", context={},
            ) from exc

    @contextmanager
    def open_live(self, project_id: str) -> Iterator[ProjectHandle]:
        pid = self._validate_project_id(project_id)
        try:
            with _path_lock(
                self.catalog_migration_lock,
                exclusive=False,
                timeout=self.lock_timeout,
            ):
                self._require_legacy_repository_admission()
                with _path_lock(
                    self.repo_lock,
                    exclusive=False,
                    timeout=self.lock_timeout,
                ):
                    with _path_lock(
                        self._project_lock_path(pid),
                        exclusive=False,
                        timeout=self.lock_timeout,
                    ):
                        yield self._require_live(pid)
        except _PathLockTimeout as exc:
            raise ZeError("project_busy", "project is busy", context={"project_id": pid}) from exc

    @contextmanager
    def open_live_scan_source(
        self,
        project_id: str,
        expected_generation: int,
    ) -> Iterator[_LiveScanSource]:
        """Yield one generation- and inode-bound source under its content SH lock.

        The source is intentionally narrower than ``ProjectHandle``: its live
        pathname is pinned to one owner-controlled directory inode, and its
        exact Document head remains shared-locked across the caller's scan.
        """
        pid = self._validate_project_id(project_id)
        if (
            type(expected_generation) is not int
            or expected_generation < 1
            or expected_generation > _MAX_PROJECT_GENERATION
        ):
            raise ZeError(
                "validation_failed",
                "expected_generation must be an exact positive durable integer",
                context={"reason": "expected_generation_invalid"},
            )
        try:
            with self._open_repository_lock(exclusive=False):
                with _path_lock(
                    self._project_lock_path(pid),
                    exclusive=False,
                    timeout=self.lock_timeout,
                ):
                    # Preserve the established not-found/busy/recovery errors
                    # without touching an absent namespace.  For a catalog-live
                    # row, pin and validate the root *before* path-based marker
                    # inspection so unsafe roots receive the scanner's path-free
                    # read diagnosis rather than a write-side/copy error.
                    state = self._read_state(pid)
                    if (
                        pid in self._recovery_required
                        or state is None
                        or state.get("status") != "live"
                    ):
                        self._require_live(
                            pid,
                            expected_generation=expected_generation,
                        )
                        raise AssertionError("non-live scan source was accepted")
                    root = self.path_for(pid)
                    root_descriptor, root_identity = _open_live_scan_root(root)
                    try:
                        # Re-prove catalog/marker state after opening the root,
                        # then bind that public name to the exact opened inode.
                        _assert_live_scan_root_named(root, root_identity)
                        current = self._require_live(
                            pid,
                            expected_generation=expected_generation,
                        )
                        _assert_live_scan_root_named(current.path, root_identity)
                        document_path = current.path / "document.json"
                        with _open_live_scan_head(
                            document_path,
                            root_identity,
                        ) as head:
                            self._assert_head_identity(head, pid)

                            # These are the immediate pre-yield source-admission
                            # fences.  The same checks run in ``finally`` while
                            # the Document content SH lock is still held.
                            _assert_live_scan_root_named(
                                current.path,
                                root_identity,
                            )
                            current = self._require_live(
                                pid,
                                expected_generation=expected_generation,
                            )
                            _assert_live_scan_root_named(
                                current.path,
                                root_identity,
                            )
                            source = _LiveScanSource(
                                project_id=pid,
                                generation=current.generation,
                                document_path=document_path,
                                root_identity=root_identity,
                                head=head,
                                _seal=_LIVE_SCAN_SOURCE_SEAL,
                            )
                            try:
                                yield source
                            finally:
                                _assert_live_scan_root_named(
                                    current.path,
                                    root_identity,
                                )
                                current = self._require_live(
                                    pid,
                                    expected_generation=expected_generation,
                                )
                                _assert_live_scan_root_named(
                                    current.path,
                                    root_identity,
                                )
                    finally:
                        os.close(root_descriptor)
        except _PathLockTimeout as exc:
            raise ZeError(
                "project_busy",
                "project is busy",
                context={"project_id": pid},
            ) from exc

    @contextmanager
    def activity(self, handle: ProjectHandle) -> Iterator[None]:
        try:
            with _path_lock(
                self.catalog_migration_lock,
                exclusive=False,
                timeout=self.lock_timeout,
            ):
                self._require_legacy_repository_admission()
                with _path_lock(
                    self._project_lock_path(handle.project_id),
                    exclusive=False,
                    timeout=self.lock_timeout,
                ):
                    self._require_live(
                        handle.project_id,
                        expected_generation=handle.generation,
                    )
                    yield
        except _PathLockTimeout as exc:
            raise ZeError(
                "project_busy", "project is busy", context={"project_id": handle.project_id},
            ) from exc

    def list_live_ids(self) -> list[str]:
        ids: list[str] = []
        try:
            with self._open_repository_lock(exclusive=False):
                for state_path in sorted(self.state_root.glob("*.json")):
                    state = _read_json(state_path)
                    if not state or state.get("schema") != STATE_SCHEMA or state.get("status") != "live":
                        continue
                    pid = state.get("project_id")
                    if not isinstance(pid, str):
                        continue
                    try:
                        self._require_live(pid)
                    except ZeError as exc:
                        if exc.code == "project_recovery_required":
                            continue
                        raise
                    ids.append(pid)
        except _PathLockTimeout as exc:
            raise ZeError("project_busy", "project catalog is busy", context={}) from exc
        return sorted(ids)

    def snapshot_live(self, reader: Callable[[ProjectHandle], Any]) -> list[Any]:
        """Read every live namespace under one repository-shared snapshot.

        Topology writers require repo EX, so enumeration and each handle's
        marker/state proof cannot be interleaved with create/replace/delete.
        ``reader`` may read immutable/atomically-replaced files from the handle
        but must not mutate topology.
        """
        rows: list[Any] = []
        try:
            with self._open_repository_lock(exclusive=False):
                for state_path in sorted(self.state_root.glob("*.json")):
                    state = _read_json(state_path)
                    if not state or state.get("schema") != STATE_SCHEMA or state.get("status") != "live":
                        continue
                    pid = state.get("project_id")
                    if not isinstance(pid, str):
                        continue
                    handle = self._require_live(pid)
                    rows.append(reader(handle))
        except _PathLockTimeout as exc:
            raise ZeError("project_busy", "project catalog is busy", context={}) from exc
        return rows

    def intent_status(
        self,
        project_id: str,
        operation_id: str,
        *,
        kind: str | None = None,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Return proof-oriented status for a client-held topology intent.

        Completed operations answer from immutable outcome receipts, so later
        generations cannot erase an earlier create/duplicate/replace/delete
        proof.  ``kind`` explains the expected baseline when no operation ever
        reached this coordinator: an untouched live target means
        ``not_committed`` for delete/replace, but conflicts with create/duplicate.
        """
        pid = self._validate_project_id(project_id)
        opid = self._validate_operation_id(operation_id)
        if kind is not None and (
            not isinstance(kind, str) or kind not in TOPOLOGY_KINDS
        ):
            raise ZeError("validation_failed", "invalid project topology operation kind")
        principal_hash = (
            self._principal_hash(principal_id)
            if principal_id is not None else ""
        )
        intent_reserved = False

        def _proof(
            status: str,
            *,
            resolved_kind: str | None = None,
            reserved: bool | None = None,
            **extra: Any,
        ) -> dict[str, Any]:
            return {
                "project_id": pid,
                "intent_id": opid,
                "status": status,
                "kind": str(resolved_kind or kind or "unknown"),
                "proof_revision": 2,
                "principal_scoped": principal_id is not None,
                "intent_reserved": (
                    intent_reserved if reserved is None else reserved
                ),
                **extra,
            }
        try:
            # Reconciliation is a pure read.  In particular, an arbitrary
            # status GET must not reserve an operation id, fsync an intent file
            # or create a per-project lock inode.  The first real topology POST
            # claims principal+target+kind atomically under repo EX; an existing
            # claim is still verified here before any proof is returned.
            with self._open_repository_lock(exclusive=False):
                if principal_id is not None and kind is None:
                    raise ZeError(
                        "validation_failed",
                        "kind is required when reconciling a scoped topology intent",
                    )
                claim = self._read_principal_claim(opid)
                if principal_id is not None and claim is not None:
                    # Existing-claim path is read-only; _claim_principal only
                    # validates because the claim already exists.
                    self._claim_principal(
                        opid, principal_hash, target_id=pid, kind=str(kind or ""),
                    )
                    intent_reserved = True

                outcome = self._read_outcome(opid)
                if outcome is not None:
                    outcome_principal = str(outcome.get("principal_hash") or "")
                    if principal_id is not None:
                        if not outcome_principal:
                            raise ZeError(
                                "project_recovery_required",
                                "positive topology outcome has no principal binding",
                                context={"project_id": pid, "operation_id": opid},
                            )
                        if outcome_principal != principal_hash:
                            raise ZeError(
                                "acl_denied", "topology intent belongs to another principal",
                                context={"project_id": pid, "operation_id": opid},
                            )
                    if (
                        outcome["target_id"] != pid
                        or (kind is not None and outcome["kind"] != kind)
                    ):
                        return _proof(
                            "conflict", resolved_kind=kind or outcome["kind"], reserved=False,
                        )
                    return _proof(
                        "deleted" if outcome["kind"] == "delete" else "committed",
                        resolved_kind=outcome["kind"],
                        reserved=principal_id is not None,
                        generation=outcome["generation"],
                    )

                state = self._read_state(pid)
                live = self.path_for(pid)
                marker = self._read_marker(live)
                journal = _read_json(self._journal_path(opid))
                journal_principal = str((journal or {}).get("principal_hash") or "")
                if journal is not None and principal_id is not None:
                    if not journal_principal:
                        raise ZeError(
                            "project_recovery_required",
                            "positive topology journal has no principal binding",
                            context={"project_id": pid, "operation_id": opid},
                        )
                    if journal_principal != principal_hash:
                        raise ZeError(
                            "acl_denied", "topology intent belongs to another principal",
                            context={"project_id": pid, "operation_id": opid},
                        )
                if journal is not None and (
                    journal.get("target_id") != pid
                    or (kind is not None and journal.get("kind") != kind)
                ):
                    return _proof(
                        "conflict", resolved_kind=kind or str(journal.get("kind") or "unknown"),
                        reserved=False,
                    )
                if journal is not None and principal_id is not None:
                    intent_reserved = True
                if journal is None:
                    for pending_path in self.journal_root.glob("*.json"):
                        pending = _read_json(pending_path)
                        if pending and pending.get("target_id") == pid:
                            return _proof("conflict")
                if (
                    state
                    and state.get("status") == "live"
                    and state.get("operation_id") == opid
                    and self._marker_matches(
                        marker,
                        project_id=pid,
                        generation=state["generation"],
                        operation_id=opid,
                    )
                ):
                    if principal_id is not None and not intent_reserved:
                        raise ZeError(
                            "project_recovery_required",
                            "positive topology marker has no principal binding",
                            context={"project_id": pid, "operation_id": opid},
                        )
                    return _proof(
                        "committed",
                        resolved_kind=str((journal or {}).get("kind") or kind or "create"),
                        generation=state["generation"],
                    )
                if (
                    state
                    and state.get("status") == "deleted"
                    and state.get("operation_id") == opid
                ):
                    if principal_id is not None and not intent_reserved:
                        raise ZeError(
                            "project_recovery_required",
                            "positive topology tombstone has no principal binding",
                            context={"project_id": pid, "operation_id": opid},
                        )
                    return _proof(
                        "deleted", resolved_kind="delete", generation=state["generation"],
                    )
                if (
                    pid in self._recovery_required
                    or (state and state.get("status") == "recovery_required")
                ):
                    return _proof("repair_required")
                if journal is not None:
                    return _proof(
                        "in_progress",
                        resolved_kind=str(journal.get("kind") or kind or "unknown"),
                        reserved=principal_id is not None,
                        phase=str(journal.get("phase") or "prepared"),
                    )
                if state and state.get("status") == "quiescing":
                    return _proof("repair_required")
                if kind in ("delete", "replace"):
                    status = "not_committed"
                elif marker is not None or (state and state.get("status") == "live"):
                    status = "conflict"
                else:
                    status = "not_committed"
                return _proof(status)
        except _PathLockTimeout as exc:
            raise ZeError(
                "project_busy",
                "project intent status is temporarily busy",
                context={"project_id": pid, "operation_id": opid},
            ) from exc

    # ------------------------------------------------------------------
    # Initialization and recovery
    # ------------------------------------------------------------------

    def initialize(self, *, validate: Validate) -> None:
        """Recover journals, seed markers for legacy live directories, hide controls."""
        try:
            with self._open_repository_lock(exclusive=True):
                self._initialize_locked(validate)
        except _PathLockTimeout as exc:
            raise ZeError("project_busy", "project catalog initialization timed out", context={}) from exc

    def _initialize_locked(self, validate: Validate) -> None:
        """Run v4 recovery/adoption while barrier EX/SH and repo EX are held."""

        self._recover_locked(validate)
        journal_names = {path.stem for path in self.journal_root.glob("*.json")}
        for path in list(self.staging_root.iterdir()):
            if path.name not in journal_names:
                _remove_tree(path)
        # An orphan trash entry may be the only surviving old generation. Freeze it.
        for path in self.trash_root.iterdir():
            opid = path.name.split(".", 1)[0]
            if opid in journal_names:
                continue
            marker = self._read_marker(path)
            if marker is not None:
                with _path_lock(
                    self._project_lock_path(marker["project_id"]),
                    exclusive=True,
                    timeout=self.lock_timeout,
                ):
                    self._freeze(marker["project_id"], marker["generation"], opid)
        for root in sorted(self.projects_root.iterdir()):
            if root.name in CONTROL_NAMES:
                continue
            pid = self._validate_project_id(root.name)
            info = root.lstat()
            with _path_lock(
                self._project_lock_path(pid), exclusive=True, timeout=self.lock_timeout,
            ):
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    self._freeze(pid, 1, "adopt-invalid-root")
                    continue
                try:
                    _validate_regular_tree(root)
                except ZeError:
                    self._freeze(pid, 1, "adopt-invalid-tree")
                    continue
                if not _is_regular_file(root / "document.json"):
                    self._freeze(pid, 1, "adopt-invalid")
                    continue
                marker = self._read_marker(root)
                state = self._read_state(pid)
                if marker is None and state is None:
                    try:
                        validate(root)
                        adoption_id = self._legacy_adoption_operation_id(pid)
                        self._write_marker(root, pid, 1, adoption_id)
                        _fsync_tree(root)
                        self._write_state(pid, 1, "live", adoption_id)
                    except ZeError as exc:
                        if exc.code != "project_recovery_required":
                            raise
                        self._freeze(pid, 1, "adopt-invalid-document")
                        continue
                elif marker is None or state is None:
                    # 身份记录残缺(marker/状态行只剩一样)但目录静默 —— raw copy/部分搬运的
                    # 典型伤。此处已在 journal 恢复之后、repo EX + 项目锁内,目录不可能有在途
                    # 操作;身份记录是可从文档确定性重建的簿记。先自愈,修不了才冻结。
                    self._adopt_heal(
                        root, pid,
                        max(1, int(((marker or state) or {}).get("generation", 1))),
                        validate,
                    )
                elif not self._marker_matches(
                    marker,
                    project_id=pid,
                    generation=state["generation"],
                    operation_id=str(state.get("operation_id") or ""),
                ):
                    # 两侧都在但互相矛盾:同样先自愈;代际取 max+1,栅栏掉任何持旧代际的
                    # 过期引用(自愈即宣告新 incarnation)。
                    self._adopt_heal(
                        root, pid,
                        max(marker["generation"], state["generation"]) + 1,
                        validate,
                    )
                else:
                    # A valid marker/catalog pair is still unusable if its
                    # Document claims another namespace. Detect and freeze it
                    # during every restart reconciliation.
                    try:
                        with self._document_identity_guard(root, pid):
                            pass
                    except ZeError as exc:
                        if exc.code != "project_recovery_required":
                            raise
                        self._freeze(
                            pid,
                            state["generation"],
                            "adopt-invalid-document",
                        )
                        continue
        # Catalog rows with no corresponding live root never appear in the
        # directory walk above. Do not leave an orphaned quiescing/live row
        # permanently busy or accidentally reusable.
        for state_path in list(self.state_root.glob("*.json")):
            raw_state = _read_json(state_path)
            if not raw_state:
                continue
            pid_value = raw_state.get("project_id")
            if not isinstance(pid_value, str):
                raise ZeError(
                    "project_recovery_required",
                    "project catalog row has no valid project id",
                    context={"metadata": state_path.name},
                )
            pid = self._validate_project_id(pid_value)
            if state_path != self._state_path(pid):
                raise ZeError(
                    "project_recovery_required",
                    "project catalog row is stored under the wrong identity",
                    context={"metadata": state_path.name},
                )
            with _path_lock(
                self._project_lock_path(pid), exclusive=True, timeout=self.lock_timeout,
            ):
                state = self._read_state(pid)
                if state is None:
                    continue
                live = self.path_for(pid)
                if state["status"] == "recovery_required":
                    self._recovery_required.add(pid)
                elif state["status"] == "quiescing":
                    self._freeze(pid, state["generation"], state["operation_id"])
                elif state["status"] == "live":
                    try:
                        self._require_live(pid)
                    except ZeError:
                        self._freeze(pid, state["generation"], state["operation_id"])
                elif state["status"] == "deleted" and live.exists():
                    self._freeze(pid, state["generation"], state["operation_id"])

    def _recover_locked(self, validate: Validate) -> None:
        for journal_path in sorted(self.journal_root.glob("*.json")):
            journal = _read_json(journal_path)
            if journal is None:
                continue
            if journal.get("schema") != JOURNAL_SCHEMA or journal.get("operation_id") != journal_path.stem:
                raise ZeError(
                    "project_recovery_required",
                    "project topology journal is invalid",
                    context={"journal": journal_path.name},
                )
            pid = self._validate_project_id(journal.get("target_id"))
            kind = journal.get("kind")
            try:
                with _path_lock(
                    self._project_lock_path(pid), exclusive=True, timeout=self.lock_timeout,
                ):
                    if kind in ("create", "duplicate", "legacy_migrate"):
                        self._recover_create(journal, validate)
                    elif kind == "delete":
                        self._recover_delete(journal)
                    elif kind == "replace":
                        self._recover_replace(journal, validate)
                    else:
                        raise ZeError(
                            "project_recovery_required",
                            "unknown project topology journal kind",
                            context={"kind": str(kind)},
                        )
            except _PathLockTimeout as exc:
                raise ZeError(
                    "project_busy", "project recovery could not acquire lifecycle lock",
                    context={"project_id": pid},
                ) from exc
            except Exception:
                # Freeze only the identifiable project; unrelated healthy projects still boot.
                self._freeze(pid, int(journal.get("new_generation") or 1), journal_path.stem)
                continue

    def _recover_create(self, journal: dict[str, Any], validate: Validate) -> None:
        opid = journal["operation_id"]
        pid = self._validate_project_id(journal["target_id"])
        stage = self.staging_root / opid
        live = self.path_for(pid)
        generation = int(journal["new_generation"])
        live_marker = self._read_marker(live)
        stage_marker = self._read_marker(stage)
        if self._marker_matches(
            live_marker, project_id=pid, generation=generation, operation_id=opid,
        ):
            with self._document_identity_guard(live, pid):
                validate(live)
                _fsync_directory(self.projects_root)
                _fsync_directory(self.staging_root)
                self._write_state(pid, generation, "live", opid)
            journal["phase"] = "committed"
            self._finish_recovery_cleanup(
                journal, paths=(stage,), cleanup_sources=True,
            )
            return
        if live.exists():
            state = self._read_state(pid)
            if journal.get("phase") == "prepared" and state and state.get("status") == "live" \
                    and self._marker_matches(
                        live_marker,
                        project_id=pid,
                        generation=state["generation"],
                        operation_id=str(state.get("operation_id") or ""),
                    ):
                self._finish_recovery_cleanup(
                    journal, paths=(stage,), mark_cleanup_pending=False,
                )
                return
            raise ZeError("project_recovery_required", "create target belongs to another operation")
        if journal.get("phase") in ("ready", "promoted") and self._marker_matches(
            stage_marker, project_id=pid, generation=generation, operation_id=opid,
        ):
            with self._document_identity_guard(stage, pid):
                validate(stage)
                _fsync_tree(stage)
                os.rename(stage, live)
                _fsync_directory(self.projects_root)
                _fsync_directory(self.staging_root)
                self._write_state(pid, generation, "live", opid)
            journal["phase"] = "committed"
            self._finish_recovery_cleanup(journal, cleanup_sources=True)
            return
        if journal.get("phase") == "prepared":
            self._finish_recovery_cleanup(
                journal, paths=(stage,), mark_cleanup_pending=False,
            )
            return
        raise ZeError("project_recovery_required", "create evidence cannot be reconciled")

    def _recover_delete(self, journal: dict[str, Any]) -> None:
        opid = journal["operation_id"]
        pid = self._validate_project_id(journal["target_id"])
        live = self.path_for(pid)
        trash = self.trash_root / opid
        old_generation = int(journal["old_generation"])
        new_generation = int(journal["new_generation"])
        old_operation_id = str(journal.get("old_operation_id") or "")
        expected_old_operation = old_operation_id or None
        state = self._read_state(pid)
        if (
            not live.exists()
            and state
            and state.get("status") == "deleted"
            and state.get("generation") == new_generation
            and state.get("operation_id") == opid
        ):
            if trash.exists():
                with self._document_identity_guard(trash, pid):
                    pass
            journal["phase"] = "committed"
            try:
                self._write_journal(journal)
            except Exception:
                pass
            self._finish_recovery_cleanup(journal, paths=(trash,))
            return
        if live.exists() and not trash.exists() and journal.get("phase") == "prepared":
            marker = self._read_marker(live)
            if not self._marker_matches(
                marker,
                project_id=pid,
                generation=old_generation,
                operation_id=expected_old_operation,
            ):
                raise ZeError("project_recovery_required", "delete live generation changed")
            with self._document_identity_guard(live, pid):
                self._write_state(pid, old_generation, "live", marker["operation_id"])
            self._finish_recovery_cleanup(journal, mark_cleanup_pending=False)
            return
        if not live.exists() and trash.exists():
            marker = self._read_marker(trash)
            if not self._marker_matches(
                marker,
                project_id=pid,
                generation=old_generation,
                operation_id=expected_old_operation,
            ):
                raise ZeError("project_recovery_required", "delete trash generation is invalid")
            with self._document_identity_guard(trash, pid):
                _fsync_directory(self.projects_root)
                _fsync_directory(self.trash_root)
                self._write_state(pid, new_generation, "deleted", opid)
            journal["phase"] = "committed"
            try:
                self._write_journal(journal)
            except Exception:
                pass
            self._finish_recovery_cleanup(journal, paths=(trash,))
            return
        if not live.exists() and not trash.exists() and journal.get("phase") in ("promoted", "committed", "cleanup_pending"):
            self._write_state(pid, new_generation, "deleted", opid)
            journal["phase"] = "committed"
            self._finish_recovery_cleanup(journal)
            return
        raise ZeError("project_recovery_required", "delete evidence cannot be reconciled")

    def _recover_replace(self, journal: dict[str, Any], validate: Validate) -> None:
        opid = journal["operation_id"]
        pid = self._validate_project_id(journal["target_id"])
        live = self.path_for(pid)
        stage = self.staging_root / opid
        backup = self.trash_root / f"{opid}.backup"
        old_generation = int(journal.get("old_generation") or 0)
        new_generation = int(journal["new_generation"])
        live_marker = self._read_marker(live)
        if self._marker_matches(
            live_marker, project_id=pid, generation=new_generation, operation_id=opid,
        ):
            with self._document_identity_guard(live, pid):
                validate(live)
                _fsync_directory(self.projects_root)
                _fsync_directory(self.staging_root)
                self._write_state(pid, new_generation, "live", opid)
            journal["phase"] = "committed"
            try:
                self._write_journal(journal)
            except Exception:
                pass
            self._finish_recovery_cleanup(journal, paths=(stage, backup))
            return
        old_status = str(
            journal.get("old_status")
            or ("none" if old_generation == 0 else "live")
        )
        old_operation_id = str(journal.get("old_operation_id") or "")
        expected_old_operation = old_operation_id or None
        if old_status in ("none", "deleted") and not live.exists() and not backup.exists():
            stage_marker = self._read_marker(stage)
            if journal.get("phase") == "prepared":
                try:
                    if stage.exists() or stage.is_symlink():
                        _remove_tree_durable(stage)
                except Exception:
                    self._defer_cleanup(journal, mark_cleanup_pending=False)
                    return
                state = self._read_state(pid)
                if old_status == "none" and state and state.get("operation_id") == opid:
                    _unlink_durable(self._state_path(pid))
                elif old_status == "deleted":
                    self._write_state(
                        pid, old_generation, "deleted", old_operation_id or "recovered-delete",
                    )
                self._finish_recovery_cleanup(journal, mark_cleanup_pending=False)
                return
            if journal.get("phase") in ("ready", "source_parked", "promoted") \
                    and self._marker_matches(
                        stage_marker,
                        project_id=pid,
                        generation=new_generation,
                        operation_id=opid,
                    ):
                with self._document_identity_guard(stage, pid):
                    validate(stage)
                    _fsync_tree(stage)
                    os.rename(stage, live)
                    _fsync_directory(self.projects_root)
                    _fsync_directory(self.staging_root)
                    self._write_state(pid, new_generation, "live", opid)
                journal["phase"] = "committed"
                self._finish_recovery_cleanup(journal)
                return
        if not live.exists() and backup.exists() and stage.exists():
            stage_marker = self._read_marker(stage)
            backup_marker = self._read_marker(backup)
            if self._marker_matches(
                stage_marker, project_id=pid, generation=new_generation, operation_id=opid,
            ) and self._marker_matches(
                backup_marker,
                project_id=pid,
                generation=old_generation,
                operation_id=expected_old_operation,
            ):
                with self._document_identity_guard(stage, pid):
                    validate(stage)
                    _fsync_tree(stage)
                    os.rename(stage, live)
                    _fsync_directory(self.projects_root)
                    _fsync_directory(self.staging_root)
                    self._write_state(pid, new_generation, "live", opid)
                journal["phase"] = "committed"
                try:
                    self._write_journal(journal)
                except Exception:
                    pass
                self._finish_recovery_cleanup(journal, paths=(backup,))
                return
        if not live.exists() and backup.exists() and not stage.exists():
            backup_marker = self._read_marker(backup)
            if self._marker_matches(
                backup_marker,
                project_id=pid,
                generation=old_generation,
                operation_id=expected_old_operation,
            ):
                with self._document_identity_guard(backup, pid):
                    os.rename(backup, live)
                    _fsync_directory(self.projects_root)
                    _fsync_directory(self.trash_root)
                    self._write_state(pid, old_generation, "live", backup_marker["operation_id"])
                self._finish_recovery_cleanup(journal, mark_cleanup_pending=False)
                return
        if self._marker_matches(
            live_marker,
            project_id=pid,
            generation=old_generation,
            operation_id=expected_old_operation,
        ) \
                and not backup.exists() \
                and journal.get("phase") in ("prepared", "ready", "source_parked"):
            with self._document_identity_guard(live, pid):
                _fsync_directory(self.projects_root)
                _fsync_directory(self.trash_root)
                try:
                    if stage.exists() or stage.is_symlink():
                        _remove_tree_durable(stage)
                except Exception:
                    self._defer_cleanup(journal, mark_cleanup_pending=False)
                    return
                self._write_state(pid, old_generation, "live", live_marker["operation_id"])
            self._finish_recovery_cleanup(journal, mark_cleanup_pending=False)
            return
        raise ZeError("project_recovery_required", "replace evidence cannot be reconciled")

    # ------------------------------------------------------------------
    # Topology mutations
    # ------------------------------------------------------------------

    def create(
        self,
        project_id: str,
        *,
        build: Build,
        validate: Validate,
        operation_id: str | None = None,
        kind: str = "create",
        cleanup_sources: tuple[Path, ...] = (),
        principal_id: str = "",
    ) -> TopologyCommit:
        pid = self._validate_project_id(project_id)
        opid = self._validate_operation_id(operation_id or uuid.uuid4().hex)
        stage = self.staging_root / opid
        live = self.path_for(pid)
        try:
            with self._open_repository_lock(exclusive=True):
                with _path_lock(self._project_lock_path(pid), exclusive=True, timeout=self.lock_timeout):
                    self._claim_principal(
                        opid, self._principal_hash(principal_id),
                        target_id=pid, kind=kind,
                    )
                    self._assert_no_pending_target(pid)
                    current_state = self._read_state(pid) or {}
                    if pid in self._recovery_required or current_state.get("status") == "recovery_required":
                        raise ZeError(
                            "project_recovery_required", "project id is frozen for repair",
                            context={"project_id": pid},
                        )
                    if live.exists() or current_state.get("status") in ("live", "quiescing"):
                        raise ZeError(
                            "project_topology_conflict",
                            f"project {pid!r} already exists",
                            context={"project_id": pid},
                        )
                    old_state = current_state
                    generation = int(old_state.get("generation") or 0) + 1
                    journal = self._new_journal(
                        opid, kind, target_id=pid,
                        old_generation=int(old_state.get("generation") or 0),
                        new_generation=generation,
                        old_status=str(old_state.get("status") or "none"),
                        old_operation_id=str(old_state.get("operation_id") or ""),
                        cleanup_sources=cleanup_sources,
                        principal_id=principal_id,
                    )
                    self._claim_journal(journal)
                    owned_stage = False
                    try:
                        stage.mkdir(mode=0o700, parents=False, exist_ok=False)
                        owned_stage = True
                        os.chmod(stage, 0o700)
                        build(stage)
                        if not (stage / "document.json").is_file():
                            raise ZeError("project_topology_write_failed", "staging project has no document.json")
                        self._write_marker(stage, pid, generation, opid)
                        validate(stage)
                        _fsync_tree(stage)
                        journal["phase"] = "ready"
                        self._write_journal(journal)
                        if live.exists():
                            raise ZeError("project_topology_conflict", "project target appeared during create")
                        os.rename(stage, live)
                    except BaseException:
                        if not live.exists():
                            if owned_stage:
                                _remove_tree_durable(stage)
                            self._finish_journal(opid)
                        raise
                    try:
                        _fsync_directory(self.projects_root)
                        _fsync_directory(self.staging_root)
                        journal["phase"] = "promoted"
                        self._write_journal(journal)
                        self._write_state(pid, generation, "live", opid)
                        journal["phase"] = "committed"
                        self._write_journal(journal)
                    except Exception as exc:
                        raise ZeError(
                            "project_topology_durability_unknown",
                            "project namespace changed but durability is unknown",
                            context={"project_id": pid, "operation_id": opid},
                        ) from exc
                    cleanup_pending = False
                    try:
                        self._cleanup_sources(journal)
                        self._finish_journal(opid)
                    except Exception:
                        cleanup_pending = True
                        try:
                            journal["phase"] = "cleanup_pending"
                            self._write_journal(journal)
                        except Exception:
                            pass
                    return TopologyCommit(opid, pid, generation, cleanup_pending)
        except _PathLockTimeout as exc:
            raise ZeError("project_busy", "project topology lock timed out", context={"project_id": pid}) from exc
        except ZeError:
            raise
        except Exception as exc:
            if live.exists() and (self._read_marker(live) or {}).get("operation_id") == opid:
                raise ZeError(
                    "project_topology_durability_unknown",
                    "project may already be visible",
                    context={"project_id": pid, "operation_id": opid},
                ) from exc
            raise ZeError(
                "project_topology_write_failed",
                "project could not be created",
                context={"project_id": pid},
            ) from exc

    def duplicate(
        self,
        source_id: str,
        target_id: str,
        *,
        rewrite: Rewrite,
        validate: Validate,
        operation_id: str | None = None,
        expected_generation: int | None = None,
        verify_source: VerifyExisting | None = None,
        principal_id: str = "",
    ) -> TopologyCommit:
        source = self._validate_project_id(source_id)
        target = self._validate_project_id(target_id)
        if source == target:
            raise ZeError("project_topology_conflict", "source and target project must differ")
        opid = self._validate_operation_id(operation_id or uuid.uuid4().hex)
        stage = self.staging_root / opid
        source_root = self.path_for(source)
        target_root = self.path_for(target)
        lock_ids = sorted((source, target))
        try:
            with self._open_repository_lock(exclusive=True):
                with _path_lock(self._project_lock_path(lock_ids[0]), exclusive=True, timeout=self.lock_timeout):
                    with _path_lock(self._project_lock_path(lock_ids[1]), exclusive=True, timeout=self.lock_timeout):
                        self._claim_principal(
                            opid, self._principal_hash(principal_id),
                            target_id=target, kind="duplicate",
                        )
                        self._assert_no_pending_target(target)
                        source_handle = self._require_live(
                            source, expected_generation=expected_generation,
                        )
                        # Lock order is topology lifecycle -> document content.
                        # Keep the source SH lock through authorization, safe
                        # copy, validation and target namespace publication so
                        # one duplicate can never combine two durable heads.
                        with open_document_head(
                            source_handle.path / "document.json",
                        ) as source_head:
                            assert source_head is not None
                            self._assert_head_identity(source_head, source)
                            if verify_source is not None:
                                verify_source(source_handle.path)
                            target_state = self._read_state(target) or {}
                            if target in self._recovery_required or target_state.get("status") == "recovery_required":
                                raise ZeError(
                                    "project_recovery_required", "project id is frozen for repair",
                                    context={"project_id": target},
                                )
                            if target_root.exists() or target_state.get("status") in ("live", "quiescing"):
                                raise ZeError(
                                    "project_topology_conflict",
                                    f"project {target!r} already exists",
                                    context={"project_id": target},
                                )
                            old_state = target_state
                            generation = int(old_state.get("generation") or 0) + 1
                            journal = self._new_journal(
                                opid, "duplicate", source_id=source, target_id=target,
                                old_generation=int(old_state.get("generation") or 0),
                                new_generation=generation,
                                old_status=str(old_state.get("status") or "none"),
                                old_operation_id=str(old_state.get("operation_id") or ""),
                                principal_id=principal_id,
                            )
                            self._claim_journal(journal)
                            owned_stage = False
                            try:
                                stage.mkdir(mode=0o700, parents=False, exist_ok=False)
                                owned_stage = True
                                os.chmod(stage, 0o700)
                                self.copy_durable_source(source_handle.path, stage)
                                rewrite(stage)
                                self._write_marker(stage, target, generation, opid)
                                validate(stage)
                                _fsync_tree(stage)
                                journal["phase"] = "ready"
                                self._write_journal(journal)
                                if target_root.exists():
                                    raise ZeError("project_topology_conflict", "duplicate target appeared")
                                os.rename(stage, target_root)
                            except BaseException:
                                if not target_root.exists():
                                    if owned_stage:
                                        _remove_tree_durable(stage)
                                    self._finish_journal(opid)
                                raise
                        try:
                            _fsync_directory(self.projects_root)
                            _fsync_directory(self.staging_root)
                            journal["phase"] = "promoted"
                            self._write_journal(journal)
                            self._write_state(target, generation, "live", opid)
                            journal["phase"] = "committed"
                            self._write_journal(journal)
                        except Exception as exc:
                            raise ZeError(
                                "project_topology_durability_unknown",
                                "duplicate may already be visible",
                                context={"project_id": target, "operation_id": opid},
                            ) from exc
                        cleanup_pending = False
                        try:
                            self._finish_journal(opid)
                        except Exception:
                            cleanup_pending = True
                        return TopologyCommit(opid, target, generation, cleanup_pending)
        except _PathLockTimeout as exc:
            raise ZeError("project_busy", "project topology lock timed out", context={}) from exc
        except ZeError:
            raise
        except Exception as exc:
            if target_root.exists() and (self._read_marker(target_root) or {}).get("operation_id") == opid:
                raise ZeError(
                    "project_topology_durability_unknown", "duplicate may already be visible",
                    context={"project_id": target, "operation_id": opid},
                ) from exc
            raise ZeError(
                "project_topology_write_failed", "project could not be duplicated",
                context={"project_id": target},
            ) from exc

    def replace(
        self,
        project_id: str,
        *,
        build: Build,
        validate: Validate,
        verify_existing: VerifyExisting | None = None,
        quiesce: Quiesce | None = None,
        operation_id: str | None = None,
        expected_generation: int | None = None,
        principal_id: str = "",
    ) -> TopologyCommit:
        pid = self._validate_project_id(project_id)
        opid = self._validate_operation_id(operation_id or uuid.uuid4().hex)
        stage = self.staging_root / opid
        backup = self.trash_root / f"{opid}.backup"
        live = self.path_for(pid)
        try:
            with self._open_repository_lock(exclusive=True):
                self._claim_principal(
                    opid, self._principal_hash(principal_id),
                    target_id=pid, kind="replace",
                )
                self._assert_no_pending_target(pid)
                state = self._read_state(pid)
                old_generation = int((state or {}).get("generation") or 0)
                if pid in self._recovery_required or (state and state.get("status") == "recovery_required"):
                    raise ZeError("project_recovery_required", "project requires repair")
                if state and state.get("status") == "quiescing":
                    raise ZeError("project_busy", "project already has a lifecycle transition")
                if state and state.get("status") == "live" and not live.exists():
                    raise ZeError("project_recovery_required", "live project directory is missing")
                if expected_generation is not None:
                    if expected_generation == 0:
                        if old_generation != 0 or live.exists():
                            raise ZeError(
                                "project_generation_stale",
                                "project already exists; reset requires its current generation",
                                context={
                                    "project_id": pid,
                                    "expected_generation": 0,
                                    "current_generation": old_generation,
                                },
                            )
                    else:
                        self._require_live(
                            pid, expected_generation=expected_generation,
                        )
                generation = old_generation + 1
                journal = self._new_journal(
                    opid, "replace", target_id=pid,
                    old_generation=old_generation, new_generation=generation,
                    old_status=str((state or {}).get("status") or "none"),
                    old_operation_id=str((state or {}).get("operation_id") or ""),
                    principal_id=principal_id,
                )
                self._claim_journal(journal)
                owned_stage = False
                try:
                    stage.mkdir(mode=0o700, parents=False, exist_ok=False)
                    owned_stage = True
                    os.chmod(stage, 0o700)
                    build(stage)
                    if not (stage / "document.json").is_file():
                        raise ZeError("project_topology_write_failed", "replacement has no document.json")
                    self._write_marker(stage, pid, generation, opid)
                    validate(stage)
                    _fsync_tree(stage)
                    journal["phase"] = "ready"
                    self._write_journal(journal)
                except BaseException:
                    if owned_stage:
                        _remove_tree_durable(stage)
                    self._finish_journal(opid)
                    raise

                def _restore_pre_cutover() -> None:
                    _remove_tree_durable(stage)
                    if state is None:
                        _unlink_durable(self._state_path(pid))
                    else:
                        self._write_state(
                            pid,
                            old_generation,
                            str(state.get("status") or "deleted"),
                            str(state.get("operation_id") or ""),
                        )
                    self._finish_journal(opid)

                self._write_state(pid, max(1, old_generation or generation), "quiescing", opid)
                try:
                    quiesced = quiesce is None or quiesce(self.lock_timeout)
                except BaseException as exc:
                    try:
                        _restore_pre_cutover()
                    except BaseException as cleanup_error:
                        self._freeze(pid, max(1, generation), opid)
                        raise ZeError(
                            "project_recovery_required",
                            "replacement could not restore pre-cutover state",
                            context={"project_id": pid, "operation_id": opid},
                        ) from cleanup_error
                    raise exc
                if not quiesced:
                    _restore_pre_cutover()
                    raise ZeError(
                        "project_busy", "project workers did not quiesce",
                        context={"project_id": pid},
                    )

                source_parked = False
                promoted = False
                try:
                    with _path_lock(self._project_lock_path(pid), exclusive=True, timeout=self.lock_timeout):
                        had_live = live.exists()
                        content_guard = (
                            open_document_head(live / "document.json")
                            if had_live else nullcontext(None)
                        )
                        # Lifecycle EX is acquired before content SH.  The
                        # verified old head cannot change before the namespace
                        # swap publishes its replacement.
                        with content_guard as live_head:
                            if had_live:
                                assert live_head is not None
                                self._assert_head_identity(live_head, pid)
                                marker = self._read_marker(live)
                                if not self._marker_matches(
                                    marker,
                                    project_id=pid,
                                    generation=old_generation,
                                    operation_id=str((state or {}).get("operation_id") or ""),
                                ):
                                    raise ZeError(
                                        "project_generation_stale", "project changed during replacement",
                                    )
                                if verify_existing is not None:
                                    verify_existing(live)
                                os.rename(live, backup)
                                source_parked = True
                                _fsync_directory(self.projects_root)
                                _fsync_directory(self.trash_root)
                                journal["phase"] = "source_parked"
                                self._write_journal(journal)
                            elif old_generation:
                                if not state or state.get("status") != "deleted":
                                    raise ZeError(
                                        "project_generation_stale", "project disappeared during replacement",
                                    )
                            # The replacement was validated before quiescing, but
                            # re-prove its id at the actual namespace cutover.
                            # This prevents a stale/misbound stage from receiving
                            # a marker-backed live name after the wait.
                            with self._document_identity_guard(stage, pid):
                                try:
                                    os.rename(stage, live)
                                except BaseException as promote_error:
                                    if backup.exists() and not live.exists():
                                        try:
                                            os.rename(backup, live)
                                            _fsync_directory(self.projects_root)
                                            _fsync_directory(self.trash_root)
                                            old_marker = self._read_marker(live)
                                            self._write_state(
                                                pid, old_generation, "live",
                                                str((old_marker or {}).get("operation_id") or "rollback"),
                                            )
                                            _remove_tree_durable(stage)
                                            self._finish_journal(opid)
                                            source_parked = False
                                        except BaseException as rollback_error:
                                            self._freeze(pid, max(1, generation), opid)
                                            raise ZeError(
                                                "project_recovery_required",
                                                "replacement promotion and rollback both failed",
                                                context={"project_id": pid, "operation_id": opid},
                                            ) from rollback_error
                                    raise promote_error
                                promoted = True
                                _fsync_directory(self.projects_root)
                                _fsync_directory(self.staging_root)
                                journal["phase"] = "promoted"
                                self._write_journal(journal)
                                self._write_state(pid, generation, "live", opid)
                except _PathLockTimeout as exc:
                    if source_parked or promoted:
                        raise ZeError(
                            "project_topology_durability_unknown",
                            "replacement namespace may already have changed",
                            context={"project_id": pid, "operation_id": opid},
                        ) from exc
                    _restore_pre_cutover()
                    raise ZeError("project_busy", "project activity did not stop", context={"project_id": pid}) from exc
                except BaseException as exc:
                    if source_parked or promoted:
                        raise ZeError(
                            "project_topology_durability_unknown",
                            "replacement namespace changed but durability is unknown",
                            context={"project_id": pid, "operation_id": opid},
                        ) from exc
                    try:
                        _restore_pre_cutover()
                    except BaseException as cleanup_error:
                        self._freeze(pid, max(1, generation), opid)
                        raise ZeError(
                            "project_recovery_required",
                            "replacement validation failed and cleanup could not be proven",
                            context={"project_id": pid, "operation_id": opid},
                        ) from cleanup_error
                    if isinstance(exc, ZeError):
                        raise
                    raise ZeError(
                        "project_topology_write_failed", "project replacement failed before cutover",
                        context={"project_id": pid},
                    ) from exc

                journal["phase"] = "committed"
                try:
                    self._write_journal(journal)
                except Exception as exc:
                    # live marker + catalog state already prove the logical
                    # replacement.  Keep the promoted journal/backup for
                    # startup cleanup and return success-with-cleanup-pending.
                    try:
                        proven = self._require_live(pid, expected_generation=generation)
                    except ZeError as proof_error:
                        raise ZeError(
                            "project_topology_durability_unknown",
                            "replacement committed but its durable proof is incomplete",
                            context={"project_id": pid, "operation_id": opid},
                        ) from proof_error
                    if proven.generation != generation:
                        raise ZeError(
                            "project_topology_durability_unknown",
                            "replacement generation could not be proven",
                            context={"project_id": pid, "operation_id": opid},
                        ) from exc
                    return TopologyCommit(opid, pid, generation, True)
                cleanup_pending = False
                if backup.exists():
                    try:
                        _remove_tree_durable(backup)
                    except Exception:
                        cleanup_pending = True
                try:
                    if cleanup_pending:
                        journal["phase"] = "cleanup_pending"
                        self._write_journal(journal)
                    else:
                        self._finish_journal(opid)
                except Exception:
                    # State+live marker already prove the logical commit. Leave
                    # evidence for startup cleanup, but do not report failure.
                    cleanup_pending = True
                return TopologyCommit(opid, pid, generation, cleanup_pending)
        except _PathLockTimeout as exc:
            raise ZeError("project_busy", "project topology lock timed out", context={"project_id": pid}) from exc

    def delete(
        self,
        project_id: str,
        *,
        quiesce: Quiesce | None = None,
        operation_id: str | None = None,
        expected_generation: int | None = None,
        verify_existing: VerifyExisting | None = None,
        principal_id: str = "",
    ) -> TopologyCommit:
        pid = self._validate_project_id(project_id)
        opid = self._validate_operation_id(operation_id or uuid.uuid4().hex)
        live = self.path_for(pid)
        trash = self.trash_root / opid
        try:
            with self._open_repository_lock(exclusive=True):
                self._claim_principal(
                    opid, self._principal_hash(principal_id),
                    target_id=pid, kind="delete",
                )
                self._assert_no_pending_target(pid)
                handle = self._require_live(
                    pid, expected_generation=expected_generation,
                )
                live_marker = self._read_marker(live)
                generation = handle.generation + 1
                journal = self._new_journal(
                    opid, "delete", target_id=pid,
                    old_generation=handle.generation, new_generation=generation,
                    old_status="live",
                    old_operation_id=str(live_marker.get("operation_id") or ""),
                    principal_id=principal_id,
                )
                self._claim_journal(journal)
                self._write_state(pid, handle.generation, "quiescing", opid)
                try:
                    quiesced = quiesce is None or quiesce(self.lock_timeout)
                except BaseException as exc:
                    self._write_state(pid, handle.generation, "live", live_marker["operation_id"])
                    self._finish_journal(opid)
                    raise exc
                if not quiesced:
                    self._write_state(pid, handle.generation, "live", live_marker["operation_id"])
                    self._finish_journal(opid)
                    raise ZeError(
                        "project_busy", "project workers did not quiesce",
                        context={"project_id": pid},
                    )
                source_parked = False
                try:
                    with _path_lock(self._project_lock_path(pid), exclusive=True, timeout=self.lock_timeout):
                        # Keep lifecycle EX -> content SH ordering identical to
                        # replace/duplicate.  ACL/name verification and the
                        # destructive namespace rename consume one exact head.
                        with open_document_head(live / "document.json") as live_head:
                            assert live_head is not None
                            self._assert_head_identity(live_head, pid)
                            marker = self._read_marker(live)
                            if not self._marker_matches(
                                marker,
                                project_id=pid,
                                generation=handle.generation,
                                operation_id=live_marker["operation_id"],
                            ):
                                raise ZeError("project_generation_stale", "project changed during delete")
                            if verify_existing is not None:
                                verify_existing(live)
                            os.rename(live, trash)
                            source_parked = True
                            _fsync_directory(self.projects_root)
                            _fsync_directory(self.trash_root)
                            journal["phase"] = "promoted"
                            self._write_journal(journal)
                            self._write_state(pid, generation, "deleted", opid)
                            journal["phase"] = "committed"
                            self._write_journal(journal)
                except _PathLockTimeout as exc:
                    if source_parked:
                        raise ZeError(
                            "project_topology_durability_unknown",
                            "project delete may already have changed the namespace",
                            context={"project_id": pid, "operation_id": opid},
                        ) from exc
                    self._write_state(pid, handle.generation, "live", live_marker["operation_id"])
                    self._finish_journal(opid)
                    raise ZeError("project_busy", "project activity did not stop", context={"project_id": pid}) from exc
                except BaseException as exc:
                    if source_parked or (not live.exists() and trash.exists()):
                        raise ZeError(
                            "project_topology_durability_unknown",
                            "project was moved to trash but durability is unknown",
                            context={"project_id": pid, "operation_id": opid},
                        ) from exc
                    self._write_state(pid, handle.generation, "live", live_marker["operation_id"])
                    self._finish_journal(opid)
                    if isinstance(exc, ZeError):
                        raise
                    raise ZeError(
                        "project_topology_write_failed", "project delete failed before cutover",
                        context={"project_id": pid},
                    ) from exc

                cleanup_pending = False
                try:
                    _remove_tree_durable(trash)
                except Exception:
                    cleanup_pending = True
                try:
                    if cleanup_pending:
                        journal["phase"] = "cleanup_pending"
                        self._write_journal(journal)
                    else:
                        self._finish_journal(opid)
                except Exception:
                    cleanup_pending = True
                return TopologyCommit(opid, pid, generation, cleanup_pending)
        except _PathLockTimeout as exc:
            raise ZeError("project_busy", "project topology lock timed out", context={"project_id": pid}) from exc

    # ------------------------------------------------------------------
    # Copy and journal helpers
    # ------------------------------------------------------------------

    def _rewrite_staged_document_metadata(
        self,
        staging: Path,
        *,
        expected_version: int,
        id: str,
        name: str,
        acl: dict[str, Any],
        config: dict[str, Any],
    ) -> DocumentMetadata:
        """Apply the private metadata rewrite only to this duplicate's stage.

        ``Document`` requires a module-private exact-identity capability because
        a metadata-only rewrite must never become a live mutation path that
        skips version/OpLog semantics.  This wrapper is the sole internal holder:
        it proves a direct, real staging child and matching prepared duplicate
        journal before delegating to the domain boundary.
        """
        stage = Path(staging).absolute()
        if stage.parent != self.staging_root:
            raise ZeError(
                "topology_metadata_rewrite_forbidden",
                "metadata rewrite target is not a topology staging directory",
                context={"reason": "topology_staging_path_required"},
            )
        try:
            operation_id = self._validate_operation_id(stage.name)
        except ZeError:
            raise ZeError(
                "topology_metadata_rewrite_forbidden",
                "metadata rewrite target is not an owned topology stage",
                context={"reason": "topology_staging_operation_invalid"},
            ) from None
        journal = _read_json(self._journal_path(operation_id))
        if (
            journal is None
            or journal.get("schema") != JOURNAL_SCHEMA
            or journal.get("operation_id") != operation_id
            or journal.get("kind") != "duplicate"
            or journal.get("phase") != "prepared"
        ):
            raise ZeError(
                "topology_metadata_rewrite_forbidden",
                "metadata rewrite target has no prepared duplicate authority",
                context={"reason": "topology_staging_journal_required"},
            )

        root_fd = self._open_copy_root(self.staging_root, role="staging")
        stage_fd = -1
        try:
            try:
                stage_fd = os.open(
                    operation_id,
                    _secure_directory_flags(),
                    dir_fd=root_fd,
                )
            except OSError as exc:
                raise ZeError(
                    "topology_metadata_rewrite_forbidden",
                    "metadata rewrite staging directory is unsafe",
                    context={"reason": "topology_staging_directory_unsafe"},
                ) from exc
            stage_info = os.fstat(stage_fd)
            _validate_copy_inode(
                stage_info, entry="staging", expected_kind="directory",
            )
            named = os.stat(
                operation_id,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if (named.st_dev, named.st_ino) != (stage_info.st_dev, stage_info.st_ino):
                raise ZeError(
                    "topology_metadata_rewrite_forbidden",
                    "metadata rewrite staging directory changed",
                    context={"reason": "topology_staging_directory_replaced"},
                )
            stage_identity = (stage_info.st_dev, stage_info.st_ino)
        finally:
            if stage_fd >= 0:
                os.close(stage_fd)
            os.close(root_fd)

        return _rewrite_document_metadata_for_topology_staging(
            stage / "document.json",
            expected_version=expected_version,
            id=id,
            name=name,
            acl=acl,
            config=config,
            _capability=_TOPOLOGY_STAGING_METADATA_CAPABILITY,
            _expected_root_identity=stage_identity,
        )

    def _open_copy_root(self, root: Path, *, role: str) -> int:
        """Open and pin one copy root without resolving a final symlink."""
        try:
            fd = os.open(str(Path(root).absolute()), _secure_directory_flags())
        except OSError as exc:
            raise _copy_error(f"project {role} root is unsafe or unavailable") from exc
        try:
            info = os.fstat(fd)
            _validate_copy_inode(info, entry=role, expected_kind="directory")
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _source_entry_stat(parent_fd: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _copy_error(
                "project source entry is unsafe or unavailable", entry=name,
            ) from exc

    @staticmethod
    def _open_verified_source(
        parent_fd: int,
        name: str,
        before: os.stat_result,
        *,
        expected_kind: str,
    ) -> tuple[int, os.stat_result]:
        flags = (
            _secure_directory_flags()
            if expected_kind == "directory"
            else _secure_regular_flags()
        )
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise _copy_error(
                "project source entry changed or is unsafe", entry=name,
            ) from exc
        try:
            opened = os.fstat(fd)
            _validate_copy_inode(
                opened,
                entry=name,
                expected_kind=expected_kind,
                require_single_link=(expected_kind == "regular"),
            )
            if not _same_copy_snapshot(before, opened):
                raise _copy_error("project source entry changed during admission", entry=name)
        except BaseException:
            os.close(fd)
            raise
        return fd, opened

    @staticmethod
    def _validate_entry_name(name: str) -> str:
        if (
            type(name) is not str
            or not name
            or name in (".", "..")
            or "/" in name
            or "\x00" in name
        ):
            raise _copy_error("project source contains an invalid entry name")
        return name

    def _copy_regular_fd(
        self,
        source_parent_fd: int,
        target_parent_fd: int,
        name: str,
        before: os.stat_result,
        *,
        depth: int,
        budget: _CopyBudget,
    ) -> None:
        budget.admit(before, depth=depth)
        _validate_copy_inode(
            before, entry=name, expected_kind="regular", require_single_link=True,
        )
        source_fd, opened = self._open_verified_source(
            source_parent_fd, name, before, expected_kind="regular",
        )
        target_fd = -1
        target_created = False
        try:
            flags = (
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                target_fd = os.open(name, flags, 0o600, dir_fd=target_parent_fd)
                target_created = True
            except OSError as exc:
                raise _copy_error(
                    "project copy target entry is unsafe or already exists", entry=name,
                ) from exc
            target_info = os.fstat(target_fd)
            if not stat.S_ISREG(target_info.st_mode) or target_info.st_nlink != 1:
                raise _copy_error("project copy target file is unsafe", entry=name)
            if hasattr(os, "geteuid") and target_info.st_uid != os.geteuid():
                raise _copy_error("project copy target file has an unsafe owner", entry=name)

            remaining = opened.st_size
            while remaining:
                chunk = os.read(source_fd, min(_COPY_CHUNK_BYTES, remaining))
                if not chunk:
                    raise _copy_error("project source file changed while copying", entry=name)
                remaining -= len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    if written <= 0:  # pragma: no cover - os.write contract guard
                        raise OSError(errno.EIO, "short project copy write")
                    view = view[written:]
            if os.read(source_fd, 1):
                raise _copy_error("project source file changed while copying", entry=name)
            if not _same_copy_snapshot(opened, os.fstat(source_fd)):
                raise _copy_error("project source file changed while copying", entry=name)
            os.fchmod(target_fd, stat.S_IMODE(opened.st_mode) & 0o777)
            os.fsync(target_fd)
        except BaseException:
            if target_created:
                try:
                    os.unlink(name, dir_fd=target_parent_fd)
                except OSError:
                    pass
            raise
        finally:
            if target_fd >= 0:
                os.close(target_fd)
            os.close(source_fd)

    def _copy_directory_fd(
        self,
        source_parent_fd: int,
        target_parent_fd: int,
        name: str,
        before: os.stat_result,
        *,
        depth: int,
        budget: _CopyBudget,
    ) -> None:
        budget.admit(before, depth=depth)
        _validate_copy_inode(before, entry=name, expected_kind="directory")
        source_fd, opened = self._open_verified_source(
            source_parent_fd, name, before, expected_kind="directory",
        )
        target_fd = -1
        try:
            try:
                os.mkdir(name, 0o700, dir_fd=target_parent_fd)
                target_fd = os.open(name, _secure_directory_flags(), dir_fd=target_parent_fd)
            except OSError as exc:
                raise _copy_error(
                    "project copy target directory is unsafe or already exists", entry=name,
                ) from exc
            target_info = os.fstat(target_fd)
            if not stat.S_ISDIR(target_info.st_mode):
                raise _copy_error("project copy target directory is unsafe", entry=name)
            if hasattr(os, "geteuid") and target_info.st_uid != os.geteuid():
                raise _copy_error("project copy target directory has an unsafe owner", entry=name)
            os.fchmod(target_fd, stat.S_IMODE(opened.st_mode) & 0o777)

            try:
                with os.scandir(source_fd) as entries:
                    for entry in entries:
                        child_name = self._validate_entry_name(entry.name)
                        child = self._source_entry_stat(source_fd, child_name)
                        if child is None:
                            raise _copy_error(
                                "project source entry changed during enumeration", entry=child_name,
                            )
                        if stat.S_ISREG(child.st_mode):
                            self._copy_regular_fd(
                                source_fd,
                                target_fd,
                                child_name,
                                child,
                                depth=depth + 1,
                                budget=budget,
                            )
                        elif stat.S_ISDIR(child.st_mode):
                            self._copy_directory_fd(
                                source_fd,
                                target_fd,
                                child_name,
                                child,
                                depth=depth + 1,
                                budget=budget,
                            )
                        else:
                            raise _copy_error(
                                "project source contains a symlink or special file",
                                entry=child_name,
                            )
            except ZeError:
                raise
            except OSError as exc:
                raise _copy_error("project source directory cannot be enumerated", entry=name) from exc
            if not _same_copy_snapshot(opened, os.fstat(source_fd)):
                raise _copy_error("project source directory changed while copying", entry=name)
            os.fsync(target_fd)
        except BaseException:
            # The operation-owned staging tree is the authoritative cleanup
            # unit.  Do not recursively clean a possibly swapped path here.
            raise
        finally:
            if target_fd >= 0:
                os.close(target_fd)
            os.close(source_fd)

    def copy_durable_source(self, source: Path, target: Path) -> None:
        """Admission-guarded public durable copy primitive."""

        with self._open_repository_lock(exclusive=True):
            self._copy_durable_source_locked(source, target)

    def _copy_durable_source_locked(self, source: Path, target: Path) -> None:
        """Copy the canonical durable set through one race-resistant primitive.

        Every path component below the two pinned roots is opened relative to a
        directory descriptor with ``O_NOFOLLOW`` and checked again with
        ``fstat``.  Symlinks, hard-linked regular files, FIFOs/devices/sockets,
        foreign owners, and group/world-writable sources fail before the staging
        namespace can be published.  ``duplicate`` and root-layout migration
        intentionally share this exact implementation.

        The topology locks serialize framework writers.  A same-UID process
        that edits the legacy root outside this coordinator is not a supported
        concurrent writer: mutation of an opened inode/directory is detected
        while copying, but the journal does not lease unrelated external file
        descriptors after the snapshot and before post-publish source cleanup.
        """
        source_fd = self._open_copy_root(source, role="source")
        target_fd = -1
        budget = _CopyBudget()
        try:
            target_fd = self._open_copy_root(target, role="target")
            source_root = os.fstat(source_fd)
            target_root = os.fstat(target_fd)
            if (source_root.st_dev, source_root.st_ino) == (
                target_root.st_dev, target_root.st_ino,
            ):
                raise _copy_error("project copy source and target must differ")
            for name in self.durable_entries:
                source_entry = self._source_entry_stat(source_fd, name)
                if source_entry is None:
                    continue
                if stat.S_ISREG(source_entry.st_mode):
                    self._copy_regular_fd(
                        source_fd,
                        target_fd,
                        name,
                        source_entry,
                        depth=1,
                        budget=budget,
                    )
                elif stat.S_ISDIR(source_entry.st_mode):
                    self._copy_directory_fd(
                        source_fd,
                        target_fd,
                        name,
                        source_entry,
                        depth=1,
                        budget=budget,
                    )
                else:
                    raise _copy_error(
                        "project source contains a symlink or special file", entry=name,
                    )
            os.fsync(target_fd)
        finally:
            if target_fd >= 0:
                os.close(target_fd)
            os.close(source_fd)

    def read_durable_source_file(
        self,
        source: Path,
        name: str,
        *,
        max_bytes: int = _DURABLE_METADATA_MAX_BYTES,
    ) -> bytes | None:
        """Admission-guarded public durable metadata reader."""

        with self._open_repository_lock(exclusive=False):
            return self._read_durable_source_file_locked(
                source,
                name,
                max_bytes=max_bytes,
            )

    def _read_durable_source_file_locked(
        self,
        source: Path,
        name: str,
        *,
        max_bytes: int = _DURABLE_METADATA_MAX_BYTES,
    ) -> bytes | None:
        """Read one durable metadata file without following or racing a link."""
        if name not in self.durable_entries or Path(name).name != name:
            raise ZeError("validation_failed", "invalid durable source entry")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        source_fd = self._open_copy_root(source, role="source")
        file_fd = -1
        try:
            before = self._source_entry_stat(source_fd, name)
            if before is None:
                return None
            _validate_copy_inode(
                before, entry=name, expected_kind="regular", require_single_link=True,
            )
            file_fd, opened = self._open_verified_source(
                source_fd, name, before, expected_kind="regular",
            )
            if opened.st_size > max_bytes:
                raise _copy_error("project source metadata exceeds its byte budget", entry=name)
            chunks: list[bytes] = []
            remaining = opened.st_size
            while remaining:
                chunk = os.read(file_fd, min(_COPY_CHUNK_BYTES, remaining))
                if not chunk:
                    raise _copy_error("project source file changed while reading", entry=name)
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(file_fd, 1):
                raise _copy_error("project source file changed while reading", entry=name)
            if not _same_copy_snapshot(opened, os.fstat(file_fd)):
                raise _copy_error("project source file changed while reading", entry=name)
            return b"".join(chunks)
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            os.close(source_fd)

    def present_durable_source_paths(self, source: Path) -> tuple[Path, ...]:
        """Admission-guarded public durable source enumeration."""

        with self._open_repository_lock(exclusive=False):
            return self._present_durable_source_paths_locked(source)

    def _present_durable_source_paths_locked(
        self,
        source: Path,
    ) -> tuple[Path, ...]:
        """Snapshot which canonical root entries are eligible for later cleanup."""
        root = Path(source).absolute()
        source_fd = self._open_copy_root(root, role="source")
        try:
            present = [
                root / name
                for name in self.durable_entries
                if self._source_entry_stat(source_fd, name) is not None
            ]
            return tuple(present)
        finally:
            os.close(source_fd)

    def _new_journal(
        self,
        operation_id: str,
        kind: str,
        *,
        target_id: str,
        old_generation: int,
        new_generation: int,
        source_id: str = "",
        old_status: str = "none",
        old_operation_id: str = "",
        cleanup_sources: tuple[Path, ...] = (),
        principal_id: str = "",
    ) -> dict[str, Any]:
        operation_id = self._validate_operation_id(operation_id)
        if not isinstance(kind, str) or kind not in TOPOLOGY_KINDS:
            raise ZeError("validation_failed", "invalid project topology operation kind")
        if old_status not in ("none", "live", "deleted"):
            raise ZeError("validation_failed", "invalid previous project status")
        if not isinstance(old_operation_id, str):
            raise ZeError("validation_failed", "invalid previous operation id")
        cleanup_names: list[str] = []
        for source in cleanup_sources:
            source = Path(source).absolute()
            if source.parent != self.data_root or source.name not in ("document.json", "entities", "assets"):
                raise ZeError("validation_failed", "invalid legacy cleanup source")
            cleanup_names.append(source.name)
        return {
            "schema": JOURNAL_SCHEMA,
            "operation_id": operation_id,
            "kind": kind,
            "source_id": source_id,
            "target_id": target_id,
            "old_generation": old_generation,
            "new_generation": new_generation,
            "old_status": old_status,
            "old_operation_id": old_operation_id,
            "phase": "prepared",
            "cleanup_sources": cleanup_names,
            "principal_hash": self._principal_hash(principal_id),
        }

    def _cleanup_sources(self, journal: dict[str, Any]) -> None:
        for name in journal.get("cleanup_sources") or []:
            if name not in ("document.json", "entities", "assets"):
                raise ZeError("project_recovery_required", "journal cleanup source is invalid")
            source = self.data_root / name
            if source.exists() or source.is_symlink():
                _remove_tree(source)
        _fsync_directory(self.data_root)


__all__ = [
    "PROJECT_MARKER",
    "ProjectHandle",
    "ProjectTopologyCoordinator",
    "TopologyCommit",
]
