"""Privileged local software-update transaction for ZE runtimes.

This module is deliberately a local companion API, not an HTTP route.  A web
page may display an update offer, but it must never receive a primitive that can
write an executable installation directory.  The trusted Workspace/launcher
constructs :class:`LocalUpdateAgent` with pre-installed release keys and calls
it after System has resolved policy and the human has chosen an action.

The agent owns only software bytes below ``root``.  It has no project-path
parameter and never performs project-schema migrations.  Directory components
are installed into immutable version directories and activated through an
atomic ``current.json`` pointer.  A failed post-switch health check restores the
previous pointer before returning a receipt.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import IO, Any, Callable, Iterable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

try:  # pragma: no cover - platform branch
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - platform branch
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]


STATE_SCHEMA = "ze.local-update-state.v1"
RECEIPT_SCHEMA = "ze.local-update-receipt.v1"
POINTER_SCHEMA = "ze.local-update-pointer.v1"
INSTALL_SCHEMA = "ze.local-update-install.v1"
TRUST_KEY_SCHEMA = "ze.local-release-key.v1"
ACTIVATION_SCHEMA = "ze.local-update-activation.v1"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCOPE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_COMPONENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_KEY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SEMVER = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?$"
)
_ROLE_BY_KIND = {
    "workspace": "runtime",
    "local-bridge": "local-adapter",
    "skill-runtime": "runtime",
    "catalog": "client",
    "theme": "client",
    "server-api": "server",
    "project-schema": "evidence",
}
_LOCAL_DIRECTORY_KINDS = frozenset(("local-bridge", "skill-runtime"))
_LOCAL_DATA_KINDS = frozenset(("catalog", "theme"))
_MAX_STATE_BYTES = 4 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 20_000
_MAX_ARCHIVE_DEPTH = 64
_MAX_UNPACKED_BYTES = 8 * 1024 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


class LocalUpdateError(RuntimeError):
    """Stable local-update failure that is safe to project to product UI."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class TrustedReleaseKey:
    """A release key installed by the launcher, never learned from a manifest."""

    key_id: str
    public_key: bytes
    series_id: str
    organization_id: str
    allowed_artifact_roles: tuple[str, ...]
    status: str = "active"
    not_before: str | None = None
    not_after: str | None = None

    def __post_init__(self) -> None:
        if _KEY_ID.fullmatch(self.key_id) is None:
            raise LocalUpdateError("trust-key-invalid")
        if not isinstance(self.public_key, bytes) or not self.public_key or len(self.public_key) > 16 * 1024:
            raise LocalUpdateError("trust-key-invalid")
        if self.status not in {"active", "retired", "revoked"}:
            raise LocalUpdateError("trust-key-invalid")
        if not self.series_id or not self.organization_id:
            raise LocalUpdateError("trust-key-invalid")
        if not self.allowed_artifact_roles:
            raise LocalUpdateError("trust-key-invalid")

    def public_record(self) -> dict[str, Any]:
        """Return non-secret key metadata suitable for local diagnostics."""

        return {
            "schema": TRUST_KEY_SCHEMA,
            "key_id": self.key_id,
            "public_key_digest": _digest_bytes(_public_key_raw(self.public_key)),
            "series_id": self.series_id,
            "organization_id": self.organization_id,
            "allowed_artifact_roles": list(self.allowed_artifact_roles),
            "status": self.status,
            "not_before": self.not_before,
            "not_after": self.not_after,
        }


@dataclass(frozen=True)
class InstallAuthorization:
    """Fresh launcher observation authorizing one activation attempt."""

    reason: str
    safe_point_confirmed: bool
    user_confirmed: bool = False

    def __post_init__(self) -> None:
        if self.reason not in {"user-confirmed", "install-on-exit", "automatic-patch"}:
            raise LocalUpdateError("install-authorization-invalid")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LocalUpdateError("manifest-invalid", f"{field} is not RFC3339") from exc
    if parsed.tzinfo is None:
        raise LocalUpdateError("manifest-invalid", f"{field} has no timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LocalUpdateError("manifest-invalid") from exc


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_file(path: Path, *, expected_size: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_COPY_CHUNK):
            total += len(chunk)
            if expected_size is not None and total > expected_size:
                raise LocalUpdateError("artifact-size-mismatch")
            digest.update(chunk)
    if expected_size is not None and total != expected_size:
        raise LocalUpdateError("artifact-size-mismatch")
    return "sha256:" + digest.hexdigest(), total


def _public_key(value: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError as exc:  # pragma: no cover - managed dependency
        raise LocalUpdateError("crypto-unavailable") from exc
    try:
        if len(value) == 32:
            return Ed25519PublicKey.from_public_bytes(value)
        key = load_pem_public_key(value)
    except (TypeError, ValueError) as exc:
        raise LocalUpdateError("trust-key-invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise LocalUpdateError("trust-key-invalid")
    return key


def _public_key_raw(value: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:  # pragma: no cover - managed dependency
        raise LocalUpdateError("crypto-unavailable") from exc
    return _public_key(value).public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _manifest_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(manifest))
    payload.pop("signature", None)
    return payload


def _object(value: Any, *, code: str = "manifest-invalid") -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LocalUpdateError(code)
    return value


def artifact_signature_message(digest: str) -> bytes:
    """The detached-signature message shared with Build Artifact Variant."""

    if _DIGEST.fullmatch(digest) is None:
        raise LocalUpdateError("artifact-digest-invalid")
    return b"ZE-ARTIFACT-SIGNATURE-V1\0" + digest.encode("ascii")


def _semver_parts(value: str) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    match = _SEMVER.fullmatch(value)
    if not match:
        raise LocalUpdateError("version-invalid")
    core = (int(match["major"]), int(match["minor"]), int(match["patch"]))
    pre = tuple(match["pre"].split(".")) if match["pre"] else None
    return core, pre


def _compare_versions(left: str, right: str) -> int:
    left_core, left_pre = _semver_parts(left)
    right_core, right_pre = _semver_parts(right)
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    if left_pre is None and right_pre is None:
        return 0
    if left_pre is None:
        return 1
    if right_pre is None:
        return -1
    for left_item, right_item in zip(left_pre, right_pre):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_item) < int(right_item) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_item < right_item else 1
    if len(left_pre) == len(right_pre):
        return 0
    return -1 if len(left_pre) < len(right_pre) else 1


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = _canonical_json(value) + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _read_json(path: Path, *, schema: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_STATE_BYTES:
            raise LocalUpdateError("update-state-invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except LocalUpdateError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LocalUpdateError("update-state-invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise LocalUpdateError("update-state-invalid")
    return value


def _empty_state() -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "generation": 0,
        "highest_manifests": {},
        "components": {},
        "updated_at": _iso(),
    }


def _scope_key(scope: Mapping[str, Any], component_id: str) -> str:
    series_id = scope.get("series_id")
    organization_id = scope.get("organization_id")
    target_id = scope.get("target_id")
    if not all(
        isinstance(value, str) and value
        for value in (series_id, organization_id, target_id)
    ):
        raise LocalUpdateError("manifest-invalid")
    return "/".join((str(series_id), str(organization_id), str(target_id), component_id))


def _safe_member_name(raw: str) -> PurePosixPath:
    normalized = raw.replace("\\", "/")
    value = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or value.is_absolute()
        or any(part in {"", ".", ".."} for part in value.parts)
        or len(value.parts) > _MAX_ARCHIVE_DEPTH
    ):
        raise LocalUpdateError("archive-path-unsafe")
    return value


def _write_stream(source: IO[bytes], destination: Path, expected: int) -> None:
    written = 0
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        while chunk := source.read(_COPY_CHUNK):
            written += len(chunk)
            if written > expected:
                raise LocalUpdateError("archive-size-invalid")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if written != expected:
        raise LocalUpdateError("archive-size-invalid")


def _unpacked_limit(artifact_size: int) -> int:
    return min(_MAX_UNPACKED_BYTES, max(64 * 1024 * 1024, artifact_size * 40))


def _extract_zip(archive_path: Path, destination: Path, artifact_size: int) -> None:
    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = archive.infolist()
        if len(infos) > _MAX_ARCHIVE_ENTRIES:
            raise LocalUpdateError("archive-entry-limit")
        names: set[str] = set()
        total = 0
        checked: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        for info in infos:
            name = _safe_member_name(info.filename)
            canonical = name.as_posix().rstrip("/")
            if not canonical or canonical in names:
                raise LocalUpdateError("archive-entry-duplicate")
            names.add(canonical)
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise LocalUpdateError("archive-entry-type-unsafe")
            if info.flag_bits & 0x1:
                raise LocalUpdateError("archive-encrypted-unsupported")
            total += info.file_size
            if total > _unpacked_limit(artifact_size):
                raise LocalUpdateError("archive-unpacked-limit")
            checked.append((info, name))
        for info, name in checked:
            target = destination.joinpath(*name.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.open(info, "r") as source:
                _write_stream(source, target, info.file_size)


def _extract_tar(archive_path: Path, destination: Path, artifact_size: int) -> None:
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        if len(members) > _MAX_ARCHIVE_ENTRIES:
            raise LocalUpdateError("archive-entry-limit")
        names: set[str] = set()
        total = 0
        checked: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        for member in members:
            name = _safe_member_name(member.name)
            canonical = name.as_posix().rstrip("/")
            if not canonical or canonical in names:
                raise LocalUpdateError("archive-entry-duplicate")
            names.add(canonical)
            if not (member.isdir() or member.isreg()):
                raise LocalUpdateError("archive-entry-type-unsafe")
            total += member.size
            if total > _unpacked_limit(artifact_size):
                raise LocalUpdateError("archive-unpacked-limit")
            checked.append((member, name))
        for member, name in checked:
            target = destination.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise LocalUpdateError("archive-entry-invalid")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with source:
                _write_stream(source, target, member.size)


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise LocalUpdateError("install-tree-symlink")
        os.chmod(path, 0o500 if path.is_dir() else 0o400)
    os.chmod(root, 0o500)


class LocalUpdateAgent:
    """Crash-aware update executor for one device-local installation root."""

    def __init__(self, root: str | Path, trusted_keys: Iterable[TrustedReleaseKey]):
        requested = Path(root).expanduser()
        if requested.exists() and requested.is_symlink():
            raise LocalUpdateError("update-root-symlink")
        requested.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(requested, 0o700)
        self.root = requested.resolve(strict=True)
        self._state_path = self.root / "state.json"
        self._lock_path = self.root / ".update.lock"
        self._activation_path = self.root / "activation.json"
        keys: dict[str, TrustedReleaseKey] = {}
        for key in trusted_keys:
            if key.key_id in keys:
                raise LocalUpdateError("trust-key-duplicate")
            keys[key.key_id] = key
        if not keys:
            raise LocalUpdateError("trust-store-empty")
        self._trusted_keys = keys
        self.recover_interrupted_activation()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        lock_name = str(self._lock_path)
        with _LOCAL_LOCKS_GUARD:
            process_lock = _LOCAL_LOCKS.setdefault(lock_name, threading.RLock())
        with process_lock:
            descriptor = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            stream = os.fdopen(descriptor, "r+b", closefd=True)
            try:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                elif msvcrt is not None:  # pragma: no cover - Windows branch
                    stream.seek(0)
                    stream.write(b"0")
                    stream.flush()
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                else:  # pragma: no cover - unsupported platform
                    raise LocalUpdateError("cross-process-lock-unavailable")
                yield
            finally:
                if fcntl is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover
                    with contextlib.suppress(OSError):
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                stream.close()

    def _load_state(self) -> dict[str, Any]:
        try:
            state = _read_json(self._state_path, schema=STATE_SCHEMA)
        except FileNotFoundError:
            return _empty_state()
        if not isinstance(state.get("generation"), int) or state["generation"] < 0:
            raise LocalUpdateError("update-state-invalid")
        if not isinstance(state.get("highest_manifests"), dict) or not isinstance(
            state.get("components"), dict
        ):
            raise LocalUpdateError("update-state-invalid")
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        state["generation"] = int(state.get("generation", 0)) + 1
        state["updated_at"] = _iso()
        _atomic_json(self._state_path, state)

    def read_state(self) -> dict[str, Any]:
        """Return a detached local state snapshot without installation paths."""

        with self._locked():
            return copy.deepcopy(self._load_state())

    def recover_interrupted_activation(self) -> dict[str, Any] | None:
        """Finish or roll back a transaction left by a terminated launcher."""

        with self._locked():
            try:
                journal = _read_json(self._activation_path, schema=ACTIVATION_SCHEMA)
            except FileNotFoundError:
                return None
            component_id = journal.get("component_id")
            version = journal.get("version")
            if _COMPONENT_ID.fullmatch(str(component_id)) is None:
                raise LocalUpdateError("activation-journal-invalid")
            _semver_parts(str(version))
            state = self._load_state()
            component_state = (state.get("components") or {}).get(component_id)
            pointer_path = self._component_dir(str(component_id)) / "current.json"
            pointer: dict[str, Any] | None = None
            if pointer_path.exists():
                pointer = _read_json(pointer_path, schema=POINTER_SCHEMA)
            versions = component_state.get("versions") if isinstance(component_state, dict) else None
            candidate = versions.get(version) if isinstance(versions, dict) else None
            already_healthy = (
                isinstance(component_state, dict)
                and component_state.get("current_version") == version
                and isinstance(candidate, dict)
                and candidate.get("status") == "healthy"
                and isinstance(pointer, dict)
                and pointer.get("component_id") == component_id
                and pointer.get("version") == version
                and pointer.get("artifact_digest") == journal.get("artifact_digest")
            )
            if already_healthy:
                self._activation_path.unlink()
                _fsync_dir(self.root)
                return self._write_receipt({
                    "action": "recover",
                    "outcome": "already-healthy",
                    "scope": copy.deepcopy(journal.get("scope") or {}),
                    "manifest_id": journal.get("manifest_id"),
                    "manifest_version": journal.get("manifest_version"),
                    "component_id": component_id,
                    "version": version,
                    "previous_version": journal.get("previous_version"),
                    "current_version": version,
                    "artifact_digest": journal.get("artifact_digest"),
                    "reason_codes": ["activation-commit-recovered"],
                })

            previous_pointer = journal.get("previous_pointer")
            if previous_pointer is None:
                with contextlib.suppress(FileNotFoundError):
                    pointer_path.unlink()
                _fsync_dir(pointer_path.parent)
            elif isinstance(previous_pointer, dict) and previous_pointer.get("schema") == POINTER_SCHEMA:
                _atomic_json(pointer_path, previous_pointer)
            else:
                raise LocalUpdateError("activation-journal-invalid")
            if isinstance(component_state, dict):
                component_state["current_version"] = journal.get("previous_version")
                if isinstance(candidate, dict):
                    candidate["status"] = "interrupted"
                self._save_state(state)
            self._activation_path.unlink()
            _fsync_dir(self.root)
            return self._write_receipt({
                "action": "recover",
                "outcome": "rolled-back",
                "scope": copy.deepcopy(journal.get("scope") or {}),
                "manifest_id": journal.get("manifest_id"),
                "manifest_version": journal.get("manifest_version"),
                "component_id": component_id,
                "version": version,
                "previous_version": journal.get("previous_version"),
                "current_version": journal.get("previous_version"),
                "artifact_digest": journal.get("artifact_digest"),
                "reason_codes": ["activation-interrupted"],
            })

    def _key_for(self, key_id: Any, manifest: Mapping[str, Any], *, at: datetime) -> TrustedReleaseKey:
        key = self._trusted_keys.get(str(key_id))
        if key is None:
            raise LocalUpdateError("release-key-untrusted")
        scope = _object(manifest.get("scope"))
        component = _object(manifest.get("component"))
        kind = component.get("kind")
        role = _ROLE_BY_KIND.get(kind) if isinstance(kind, str) else None
        if (
            key.status != "active"
            or key.series_id != scope.get("series_id")
            or key.organization_id != scope.get("organization_id")
            or role not in set(key.allowed_artifact_roles)
        ):
            raise LocalUpdateError("release-key-untrusted")
        if key.not_before and at < _timestamp(key.not_before, "trust.not_before"):
            raise LocalUpdateError("release-key-not-active")
        if key.not_after and at >= _timestamp(key.not_after, "trust.not_after"):
            raise LocalUpdateError("release-key-expired")
        return key

    def _verify_manifest(self, manifest: Mapping[str, Any], *, at: datetime) -> tuple[str, str, str]:
        if manifest.get("schema") != "ze.update-manifest.v1":
            raise LocalUpdateError("manifest-invalid")
        scope = _object(manifest.get("scope"))
        component = _object(manifest.get("component"))
        artifact = _object(manifest.get("artifact"))
        artifact_signature = _object(artifact.get("signature"))
        install = _object(manifest.get("install"))
        signature = _object(manifest.get("signature"))
        if any(
            _SCOPE_ID.fullmatch(str(scope.get(name))) is None
            for name in ("series_id", "organization_id", "target_id")
        ):
            raise LocalUpdateError("manifest-invalid")
        component_id = component.get("component_id")
        version = component.get("version")
        if _COMPONENT_ID.fullmatch(str(component_id)) is None:
            raise LocalUpdateError("manifest-invalid")
        _semver_parts(str(version))
        if component.get("kind") not in _ROLE_BY_KIND:
            raise LocalUpdateError("manifest-invalid")
        if manifest.get("state") != "active":
            raise LocalUpdateError("release-not-active")
        published_at = _timestamp(manifest.get("published_at"), "published_at")
        expires_at = _timestamp(manifest.get("expires_at"), "expires_at")
        if at < published_at:
            raise LocalUpdateError("release-not-published")
        if at >= expires_at:
            raise LocalUpdateError("manifest-expired")
        if not isinstance(manifest.get("manifest_version"), int) or manifest["manifest_version"] < 1:
            raise LocalUpdateError("manifest-invalid")
        expected_digest = artifact.get("sha256")
        if _DIGEST.fullmatch(str(expected_digest)) is None:
            raise LocalUpdateError("manifest-invalid")
        if not isinstance(artifact.get("size_bytes"), int) or artifact["size_bytes"] < 1:
            raise LocalUpdateError("manifest-invalid")
        if (
            artifact_signature.get("algorithm") != "ed25519"
            or _KEY_ID.fullmatch(str(artifact_signature.get("key_id"))) is None
            or _DIGEST.fullmatch(str(artifact_signature.get("public_key_digest"))) is None
            or _DIGEST.fullmatch(str(artifact_signature.get("signature_digest"))) is None
        ):
            raise LocalUpdateError("manifest-invalid")
        if install.get("restart") not in {"none", "recommended", "required"}:
            raise LocalUpdateError("manifest-invalid")
        parsed = urlsplit(str(artifact.get("immutable_url") or ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise LocalUpdateError("artifact-url-invalid")
        if signature.get("algorithm") != "ed25519":
            raise LocalUpdateError("manifest-signature-invalid")
        key = self._key_for(signature.get("key_id"), manifest, at=at)
        key_digest = _digest_bytes(_public_key_raw(key.public_key))
        if signature.get("public_key_digest") != key_digest:
            raise LocalUpdateError("manifest-signature-invalid")
        payload = _manifest_payload(manifest)
        payload_bytes = _canonical_json(payload)
        payload_digest = _digest_bytes(payload_bytes)
        if signature.get("payload_digest") != payload_digest:
            raise LocalUpdateError("manifest-signature-invalid")
        try:
            signed = base64.b64decode(str(signature.get("value") or ""), validate=True)
            if len(signed) != 64:
                raise LocalUpdateError("manifest-signature-invalid")
            _public_key(key.public_key).verify(signed, payload_bytes)
        except LocalUpdateError:
            raise
        except (binascii.Error, ValueError, TypeError) as exc:
            raise LocalUpdateError("manifest-signature-invalid") from exc
        except Exception as exc:
            raise LocalUpdateError("manifest-signature-invalid") from exc
        return str(component_id), str(version), payload_digest

    @staticmethod
    def _verify_offer(manifest: Mapping[str, Any], offer: Mapping[str, Any], *, action: str) -> None:
        component = manifest["component"]
        exact = {
            "schema": "ze.update-offer.v1",
            "manifest_id": manifest.get("manifest_id"),
            "manifest_version": manifest.get("manifest_version"),
            "component_id": component.get("component_id"),
            "offered_version": component.get("version"),
        }
        if any(offer.get(name) != value for name, value in exact.items()):
            raise LocalUpdateError("update-offer-mismatch")
        if offer.get("scope") != manifest.get("scope") or offer.get("decision") != "available":
            raise LocalUpdateError("update-offer-not-actionable")
        if offer.get("local_project_access_preserved") is not True:
            raise LocalUpdateError("update-offer-invalid")
        if action == "download" and offer.get("can_download") is not True:
            raise LocalUpdateError("download-not-authorized")
        if action == "install" and offer.get("can_install_now") is not True:
            raise LocalUpdateError("install-not-authorized")

    def _enforce_monotonic(
        self,
        state: Mapping[str, Any],
        manifest: Mapping[str, Any],
        component_id: str,
        version: str,
        payload_digest: str,
    ) -> None:
        key = _scope_key(manifest["scope"], component_id)
        previous = (state.get("highest_manifests") or {}).get(key)
        if isinstance(previous, dict):
            previous_number = previous.get("manifest_version")
            if manifest["manifest_version"] < previous_number:
                raise LocalUpdateError("manifest-rollback-blocked")
            if (
                manifest["manifest_version"] == previous_number
                and previous.get("payload_digest") != payload_digest
            ):
                raise LocalUpdateError("manifest-version-equivocation")
        component_state = (state.get("components") or {}).get(component_id)
        if isinstance(component_state, dict) and component_state.get("current_version"):
            current = str(component_state["current_version"])
            if _compare_versions(version, current) < 0:
                raise LocalUpdateError("software-rollback-blocked")

    def _verify_artifact_signature(
        self,
        manifest: Mapping[str, Any],
        artifact_digest: str,
        detached_signature: bytes,
        *,
        at: datetime,
    ) -> None:
        artifact = _object(manifest.get("artifact"))
        metadata = _object(artifact.get("signature"))
        if len(detached_signature) != 64:
            raise LocalUpdateError("artifact-signature-invalid")
        if metadata.get("algorithm") != "ed25519":
            raise LocalUpdateError("artifact-signature-invalid")
        if metadata.get("signature_digest") != _digest_bytes(detached_signature):
            raise LocalUpdateError("artifact-signature-invalid")
        key = self._key_for(metadata.get("key_id"), manifest, at=at)
        if metadata.get("public_key_digest") != _digest_bytes(_public_key_raw(key.public_key)):
            raise LocalUpdateError("artifact-signature-invalid")
        try:
            _public_key(key.public_key).verify(
                detached_signature,
                artifact_signature_message(artifact_digest),
            )
        except Exception as exc:
            raise LocalUpdateError("artifact-signature-invalid") from exc

    def _stage_dir(self, component_id: str, version: str) -> Path:
        return self.root / "staging" / component_id / version

    def _component_dir(self, component_id: str) -> Path:
        return self.root / "components" / component_id

    def _write_receipt(self, value: dict[str, Any]) -> dict[str, Any]:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "receipt_id": f"update-{uuid.uuid4().hex}",
            "recorded_at": _iso(),
            **value,
        }
        receipt_path = self.root / "receipts" / f"{receipt['receipt_id']}.json"
        _atomic_json(receipt_path, receipt)
        return receipt

    def stage_file(
        self,
        manifest: Mapping[str, Any],
        offer: Mapping[str, Any],
        artifact_path: str | Path,
        detached_signature: bytes,
        *,
        checked_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Copy and verify one provider-fetched artifact into local staging."""

        source = Path(artifact_path)
        if source.is_symlink() or not source.is_file():
            raise LocalUpdateError("artifact-source-invalid")
        at = checked_at or _utc_now()
        with self._locked():
            component_id, version, payload_digest = self._verify_manifest(manifest, at=at)
            self._verify_offer(manifest, offer, action="download")
            state = self._load_state()
            self._enforce_monotonic(state, manifest, component_id, version, payload_digest)
            expected_size = int(manifest["artifact"]["size_bytes"])
            staging = self._stage_dir(component_id, version)
            staging.mkdir(parents=True, exist_ok=True, mode=0o700)
            artifact = staging / "artifact.bin"
            temporary = staging / f".artifact.{uuid.uuid4().hex}.part"
            try:
                with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
                    os.chmod(temporary, 0o600)
                    copied = 0
                    while chunk := input_stream.read(_COPY_CHUNK):
                        copied += len(chunk)
                        if copied > expected_size:
                            raise LocalUpdateError("artifact-size-mismatch")
                        output_stream.write(chunk)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                actual_digest, _ = _digest_file(temporary, expected_size=expected_size)
                if actual_digest != manifest["artifact"]["sha256"]:
                    raise LocalUpdateError("artifact-digest-mismatch")
                self._verify_artifact_signature(manifest, actual_digest, detached_signature, at=at)
                if artifact.exists():
                    existing_digest, _ = _digest_file(artifact, expected_size=expected_size)
                    if existing_digest != actual_digest:
                        raise LocalUpdateError("staged-version-conflict")
                    temporary.unlink()
                else:
                    os.replace(temporary, artifact)
                    _fsync_dir(staging)
                os.chmod(artifact, 0o400)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()

            verified = {
                "schema": "ze.local-update-staged-artifact.v1",
                "scope": copy.deepcopy(manifest["scope"]),
                "manifest_id": manifest["manifest_id"],
                "manifest_version": manifest["manifest_version"],
                "manifest_payload_digest": payload_digest,
                "component_id": component_id,
                "component_kind": manifest["component"]["kind"],
                "version": version,
                "artifact_digest": manifest["artifact"]["sha256"],
                "artifact_size_bytes": expected_size,
                "verified_at": _iso(at),
            }
            _atomic_json(staging / "verified.json", verified)
            highest = state["highest_manifests"]
            highest[_scope_key(manifest["scope"], component_id)] = {
                "manifest_version": manifest["manifest_version"],
                "manifest_id": manifest["manifest_id"],
                "payload_digest": payload_digest,
            }
            components = state["components"]
            component_state = components.setdefault(
                component_id,
                {"current_version": None, "previous_version": None, "versions": {}},
            )
            component_state.setdefault("versions", {})[version] = {
                "status": "staged",
                "artifact_digest": manifest["artifact"]["sha256"],
                "manifest_id": manifest["manifest_id"],
                "manifest_version": manifest["manifest_version"],
                "component_kind": manifest["component"]["kind"],
                "verified_at": _iso(at),
            }
            self._save_state(state)
            return self._write_receipt({
                "action": "stage",
                "outcome": "verified",
                "scope": copy.deepcopy(manifest["scope"]),
                "manifest_id": manifest["manifest_id"],
                "manifest_version": manifest["manifest_version"],
                "component_id": component_id,
                "version": version,
                "artifact_digest": manifest["artifact"]["sha256"],
                "reason_codes": [],
            })

    def download_and_stage(
        self,
        manifest: Mapping[str, Any],
        offer: Mapping[str, Any],
        detached_signature: bytes,
        *,
        timeout_seconds: float = 30.0,
        checked_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Download over HTTPS with bounded streaming, then run ``stage_file``."""

        at = checked_at or _utc_now()
        component_id, version, _ = self._verify_manifest(manifest, at=at)
        self._verify_offer(manifest, offer, action="download")
        expected_size = int(manifest["artifact"]["size_bytes"])
        staging = self._stage_dir(component_id, version)
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        download = staging / f"download-{uuid.uuid4().hex}.part"
        request = Request(
            manifest["artifact"]["immutable_url"],
            headers={"Accept": "application/octet-stream", "User-Agent": "ZE-Local-Update-Agent/1"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response, download.open("xb") as output:
                final_url = urlsplit(response.geturl())
                if (
                    final_url.scheme != "https"
                    or not final_url.hostname
                    or final_url.username
                    or final_url.password
                    or final_url.fragment
                ):
                    raise LocalUpdateError("artifact-redirect-invalid")
                os.chmod(download, 0o600)
                total = 0
                while chunk := response.read(_COPY_CHUNK):
                    total += len(chunk)
                    if total > expected_size:
                        raise LocalUpdateError("artifact-size-mismatch")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if total != expected_size:
                raise LocalUpdateError("artifact-size-mismatch")
            return self.stage_file(
                manifest,
                offer,
                download,
                detached_signature,
                checked_at=at,
            )
        except LocalUpdateError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise LocalUpdateError("artifact-download-failed") from exc
        finally:
            with contextlib.suppress(FileNotFoundError):
                download.unlink()

    def _install_payload(
        self,
        manifest: Mapping[str, Any],
        staged_artifact: Path,
        destination: Path,
    ) -> None:
        kind = manifest["component"]["kind"]
        payload = destination / "payload"
        payload.mkdir(mode=0o700)
        if kind in _LOCAL_DIRECTORY_KINDS:
            try:
                if zipfile.is_zipfile(staged_artifact):
                    _extract_zip(staged_artifact, payload, manifest["artifact"]["size_bytes"])
                elif tarfile.is_tarfile(staged_artifact):
                    _extract_tar(staged_artifact, payload, manifest["artifact"]["size_bytes"])
                else:
                    raise LocalUpdateError("component-archive-required")
            except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
                raise LocalUpdateError("component-archive-invalid") from exc
        elif kind in _LOCAL_DATA_KINDS:
            shutil.copyfile(staged_artifact, payload / "artifact.bin")
            os.chmod(payload / "artifact.bin", 0o600)
        elif kind == "workspace":
            raise LocalUpdateError("platform-installer-required")
        else:
            raise LocalUpdateError("component-not-local-installable")

    @staticmethod
    def _authorize_install(offer: Mapping[str, Any], authorization: InstallAuthorization) -> None:
        if not authorization.safe_point_confirmed:
            raise LocalUpdateError("safe-point-not-confirmed")
        if authorization.reason == "user-confirmed":
            if not authorization.user_confirmed or "install-now" not in set(offer.get("actions") or []):
                raise LocalUpdateError("user-confirmation-required")
        elif authorization.reason == "install-on-exit":
            if offer.get("effective_mode") != "install-on-exit" or "install-on-exit" not in set(
                offer.get("actions") or []
            ):
                raise LocalUpdateError("install-on-exit-not-authorized")
        elif offer.get("automatic_install_now_allowed") is not True:
            raise LocalUpdateError("automatic-install-not-authorized")

    def activate(
        self,
        manifest: Mapping[str, Any],
        offer: Mapping[str, Any],
        authorization: InstallAuthorization,
        health_check: Callable[[Path], bool],
        *,
        checked_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Install, atomically activate, health-check and auto-rollback one version."""

        if not callable(health_check):
            raise LocalUpdateError("health-check-required")
        if manifest.get("project_migrations"):
            raise LocalUpdateError("project-migration-separate-confirmation-required")
        at = checked_at or _utc_now()
        with self._locked():
            component_id, version, payload_digest = self._verify_manifest(manifest, at=at)
            self._verify_offer(manifest, offer, action="install")
            self._authorize_install(offer, authorization)
            state = self._load_state()
            self._enforce_monotonic(state, manifest, component_id, version, payload_digest)
            staged = self._stage_dir(component_id, version)
            verified = _read_json(staged / "verified.json", schema="ze.local-update-staged-artifact.v1")
            if (
                verified.get("manifest_id") != manifest.get("manifest_id")
                or verified.get("manifest_version") != manifest.get("manifest_version")
                or verified.get("manifest_payload_digest") != payload_digest
                or verified.get("artifact_digest") != manifest["artifact"]["sha256"]
            ):
                raise LocalUpdateError("staged-artifact-mismatch")
            artifact = staged / "artifact.bin"
            actual_digest, _ = _digest_file(
                artifact,
                expected_size=manifest["artifact"]["size_bytes"],
            )
            if actual_digest != manifest["artifact"]["sha256"]:
                raise LocalUpdateError("staged-artifact-tampered")

            component_dir = self._component_dir(component_id)
            versions_dir = component_dir / "versions"
            versions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination = versions_dir / version
            temporary = versions_dir / f".{version}.{uuid.uuid4().hex}.installing"
            if destination.exists():
                installed = _read_json(destination / ".ze-install.json", schema=INSTALL_SCHEMA)
                if installed.get("artifact_digest") != actual_digest:
                    raise LocalUpdateError("installed-version-conflict")
            else:
                temporary.mkdir(mode=0o700)
                try:
                    self._install_payload(manifest, artifact, temporary)
                    _atomic_json(temporary / ".ze-install.json", {
                        "schema": INSTALL_SCHEMA,
                        "scope": copy.deepcopy(manifest["scope"]),
                        "manifest_id": manifest["manifest_id"],
                        "manifest_version": manifest["manifest_version"],
                        "component_id": component_id,
                        "component_kind": manifest["component"]["kind"],
                        "version": version,
                        "artifact_digest": actual_digest,
                        "installed_at": _iso(at),
                    })
                    os.replace(temporary, destination)
                    _fsync_dir(versions_dir)
                    _make_read_only(destination)
                finally:
                    if temporary.exists():
                        for path in temporary.rglob("*"):
                            with contextlib.suppress(OSError):
                                os.chmod(path, 0o700 if path.is_dir() else 0o600)
                        with contextlib.suppress(OSError):
                            os.chmod(temporary, 0o700)
                        shutil.rmtree(temporary, ignore_errors=True)

            components = state["components"]
            component_state = components.setdefault(
                component_id,
                {"current_version": None, "previous_version": None, "versions": {}},
            )
            previous = component_state.get("current_version")
            pointer_path = component_dir / "current.json"
            previous_pointer: dict[str, Any] | None = None
            if pointer_path.exists():
                previous_pointer = _read_json(pointer_path, schema=POINTER_SCHEMA)
            candidate_pointer = {
                "schema": POINTER_SCHEMA,
                "component_id": component_id,
                "version": version,
                "artifact_digest": actual_digest,
                "activated_at": _iso(at),
            }
            journal = {
                "schema": ACTIVATION_SCHEMA,
                "phase": "prepared",
                "scope": copy.deepcopy(manifest["scope"]),
                "manifest_id": manifest["manifest_id"],
                "manifest_version": manifest["manifest_version"],
                "component_id": component_id,
                "version": version,
                "artifact_digest": actual_digest,
                "previous_version": previous,
                "previous_pointer": previous_pointer,
                "candidate_pointer": candidate_pointer,
                "prepared_at": _iso(at),
            }
            _atomic_json(self._activation_path, journal)
            component_state.setdefault("versions", {}).setdefault(version, {})["status"] = "activating"
            self._save_state(state)
            _atomic_json(pointer_path, candidate_pointer)
            journal["phase"] = "switched"
            _atomic_json(self._activation_path, journal)
            try:
                healthy = health_check(destination / "payload") is True
            except Exception:
                healthy = False
            if not healthy:
                if previous_pointer is None:
                    with contextlib.suppress(FileNotFoundError):
                        pointer_path.unlink()
                    _fsync_dir(component_dir)
                else:
                    _atomic_json(pointer_path, previous_pointer)
                failed_state = self._load_state()
                failed_component = failed_state["components"][component_id]
                failed_component["versions"][version]["status"] = "unhealthy"
                failed_component["current_version"] = previous
                self._save_state(failed_state)
                receipt = self._write_receipt({
                    "action": "activate",
                    "outcome": "rolled-back",
                    "scope": copy.deepcopy(manifest["scope"]),
                    "manifest_id": manifest["manifest_id"],
                    "manifest_version": manifest["manifest_version"],
                    "component_id": component_id,
                    "version": version,
                    "previous_version": previous,
                    "current_version": previous,
                    "artifact_digest": actual_digest,
                    "reason_codes": ["health-check-failed"],
                })
                self._activation_path.unlink()
                _fsync_dir(self.root)
                return receipt

            healthy_state = self._load_state()
            healthy_component = healthy_state["components"][component_id]
            if previous and previous != version:
                healthy_component["previous_version"] = previous
            healthy_component["current_version"] = version
            healthy_component["versions"][version]["status"] = "healthy"
            healthy_component["versions"][version]["healthy_at"] = _iso()
            self._save_state(healthy_state)
            receipt = self._write_receipt({
                "action": "activate",
                "outcome": "healthy",
                "scope": copy.deepcopy(manifest["scope"]),
                "manifest_id": manifest["manifest_id"],
                "manifest_version": manifest["manifest_version"],
                "component_id": component_id,
                "version": version,
                "previous_version": previous,
                "current_version": version,
                "artifact_digest": actual_digest,
                "requires_restart": manifest["install"]["restart"] == "required",
                "reason_codes": [],
            })
            self._activation_path.unlink()
            _fsync_dir(self.root)
            return receipt

    def resolve_current(self, component_id: str) -> Path:
        """Resolve the active payload after checking pointer/state agreement."""

        if _COMPONENT_ID.fullmatch(component_id) is None:
            raise LocalUpdateError("component-id-invalid")
        with self._locked():
            state = self._load_state()
            component_state = (state.get("components") or {}).get(component_id)
            if not isinstance(component_state, dict) or not component_state.get("current_version"):
                raise LocalUpdateError("component-not-installed")
            pointer = _read_json(
                self._component_dir(component_id) / "current.json",
                schema=POINTER_SCHEMA,
            )
            version = component_state["current_version"]
            if pointer.get("component_id") != component_id or pointer.get("version") != version:
                raise LocalUpdateError("current-pointer-mismatch")
            version_state = (component_state.get("versions") or {}).get(version)
            if not isinstance(version_state, dict) or version_state.get("status") != "healthy":
                raise LocalUpdateError("current-version-unhealthy")
            destination = self._component_dir(component_id) / "versions" / version
            installed = _read_json(destination / ".ze-install.json", schema=INSTALL_SCHEMA)
            if installed.get("artifact_digest") != pointer.get("artifact_digest"):
                raise LocalUpdateError("current-pointer-mismatch")
            payload = destination / "payload"
            if payload.is_symlink() or not payload.is_dir():
                raise LocalUpdateError("current-payload-invalid")
            return payload
