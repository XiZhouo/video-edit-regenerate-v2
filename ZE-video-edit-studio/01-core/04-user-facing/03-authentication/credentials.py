"""Per-user credential vault for ZE external accounts.

This leaf extends the shared ZE-system auth directory with user-bound external
account records. The identity anchor remains `User.user_id` (usually
`feishu:<open_id>`); credentials are separate records owned by that user.

Secrets are never returned by public APIs. At rest they are stored as a sealed
blob using a host-local key file or `ZE_CREDENTIAL_MASTER_KEY`, with 0600 file
permissions and a strict, durable single-file aggregate commit.
"""

from __future__ import annotations

import base64
import copy
import errno
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from auth import (  # noqa: E402
    _exclusive_file_lock,
    default_system_auth_dir,
)
from entity import Actor, ZeError  # noqa: E402

# Authenticated encryption backend (sealed-v2).  FLEET SAFETY: this import must
# never crash a tool that synced the new core but did not rebuild its venv.  When
# `cryptography` is absent we set `_HAS_AEAD=False` and fall back to the
# hand-rolled sealed-v1 blob (read + write both stay fully functional); a store
# that only ever held v1 secrets keeps working with zero behavioural change.
try:  # pragma: no cover - import outcome is environment-driven, both paths tested
    from cryptography.hazmat.primitives import hashes as _crypto_hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF as _HKDF

    _HAS_AEAD = True
except Exception:  # noqa: BLE001 - any load failure must degrade, not crash
    _crypto_hashes = None  # type: ignore[assignment]
    _AESGCM = None  # type: ignore[assignment]
    _HKDF = None  # type: ignore[assignment]
    _HAS_AEAD = False

_LOG = logging.getLogger("ze.credentials")


CREDENTIAL_STORE_SCHEMA_VERSION = "1.0.0"
# sealed-v1: legacy hand-rolled HMAC-CTR keystream + HMAC tag (single key). Kept
# read/write-capable so no-crypto environments and existing stores never break.
CREDENTIAL_SECRET_SCHEMA_VERSION = "sealed-v1"
# sealed-v2: AES-256-GCM with an HKDF-SHA256 domain-separated subkey and the
# owning record identity as AAD.  Written whenever the AEAD backend is present.
CREDENTIAL_SECRET_SCHEMA_V2 = "sealed-v2"
CREDENTIAL_KEY_BYTES = 32
CREDENTIAL_NONCE_BYTES = 16
# 96-bit random nonce is the AES-GCM standard; distinct from the v1 nonce size.
CREDENTIAL_AEAD_NONCE_BYTES = 12
# HKDF "info" domain-separates the AEAD subkey from the raw master key (also used
# by media_ticket signing under its own domain), so the two uses never share key
# material even though both start from the same host master key.
CREDENTIAL_AEAD_INFO = b"ze-credential-aead-v2"
CREDENTIAL_AAD_VERSION = 1

_UNSUPPORTED_DIR_FSYNC = frozenset(filter(None, (
    errno.EINVAL,
    errno.EBADF,
    getattr(errno, "ENOTSUP", None),
    getattr(errno, "EOPNOTSUPP", None),
)))


class _DuplicateJsonKey(ValueError):
    pass


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateJsonKey(key)
        out[key] = value
    return out

PROVIDER_HUPO_GATEWAY = "hupo_gateway_key"
PROVIDER_OPENAI_API = "openai_api_key"
PROVIDER_ANTHROPIC_API = "anthropic_api_key"
PROVIDER_CURSOR_API = "cursor_api_key"
PROVIDER_CODEX_CLI = "codex_cli_profile"
PROVIDER_CLAUDE_CLI = "claude_cli_profile"
# AI 通道层(3.56.0):Gemini API key —— onboarding 摩擦最低的自带额度生图/LLM 路线
# (AI Studio 免费建 key、无组织验证、约 500 张/天免费额度)。
PROVIDER_GEMINI_API = "gemini_api_key"
# 系统 1b「github 仓库关联」:一份 per-user 绑定 = sealed PAT(token) + 关联元数据
# (owner/repo/ref)。token 走 secret 通道 sealed;owner/repo/ref 是非密文关联元数据。
PROVIDER_GITHUB_REPO = "github_repo"

PROVIDERS = (
    PROVIDER_HUPO_GATEWAY,
    PROVIDER_OPENAI_API,
    PROVIDER_ANTHROPIC_API,
    PROVIDER_CURSOR_API,
    PROVIDER_GEMINI_API,
    PROVIDER_CODEX_CLI,
    PROVIDER_CLAUDE_CLI,
    PROVIDER_GITHUB_REPO,
)
API_KEY_PROVIDERS = (PROVIDER_HUPO_GATEWAY, PROVIDER_OPENAI_API,
                     PROVIDER_ANTHROPIC_API, PROVIDER_CURSOR_API,
                     PROVIDER_GEMINI_API)
CLI_PROVIDERS = (PROVIDER_CODEX_CLI, PROVIDER_CLAUDE_CLI)
# 无 secret 即不可用、需绑 token 的 provider(upsert 落 needs_secret / verify 前置校验)。
# github 的 PAT 与 API key 同属「绑了 token 才算 active」,故并入。
SECRET_REQUIRED_PROVIDERS = API_KEY_PROVIDERS + (PROVIDER_GITHUB_REPO,)
DEFAULT_CAPABILITIES = {
    PROVIDER_HUPO_GATEWAY: ("llm", "embedding"),
    PROVIDER_OPENAI_API: ("llm", "image"),
    PROVIDER_ANTHROPIC_API: ("llm",),
    PROVIDER_CURSOR_API: ("llm", "agent"),
    PROVIDER_GEMINI_API: ("llm", "image"),
    # codex 原生 image_gen(gpt-image-2 底座,ChatGPT 订阅额度)已实证 —— 补 'image'
    # (AI 通道层 3.56.0;存量绑定经下方读侧并集自动获得,无需重绑)。
    PROVIDER_CODEX_CLI: ("llm", "agent", "image"),
    PROVIDER_CLAUDE_CLI: ("llm", "agent"),
    PROVIDER_GITHUB_REPO: ("file",),
}

STATUS_ACTIVE = "active"
STATUS_NEEDS_SECRET = "needs_secret"
STATUS_DISABLED = "disabled"
# A CLI binding whose OAuth blob no longer authenticates (upstream revoked it or
# the refresh failed). The runtime flips the record here so the copilot skips it
# (treated as unbound) and the account panel can offer re-login on the same cred.
STATUS_NEEDS_REAUTH = "needs_reauth"
STATUSES = (STATUS_ACTIVE, STATUS_NEEDS_SECRET, STATUS_DISABLED, STATUS_NEEDS_REAUTH)

# ---- Per-user CLI account (agentic) isolation ------------------------------
# CLI providers (claude/codex) run under the acting user's OWN subscription
# login, kept in a per-user isolated config dir so many ZE users on one server
# never cross-read or clobber each other's tokens at runtime. The isolated dir
# path is stored on the credential record's metadata; the runner exports it as
# CLAUDE_CONFIG_DIR / CODEX_HOME per subprocess.
META_HOME_DIR = "home_dir"        # absolute path of the per-user CLI config dir
META_BIN = "bin"                  # legacy server metadata; runners intentionally ignore it
META_ACCOUNT = "account"          # codex --account / profile name (optional)
META_DEFAULT = "is_default"       # this credential is the user's default AI account
# github_repo 关联元数据(非密文;owner/repo/ref 就是「关联到哪个仓库/分支」本身,
# 必须能被 public_dict / 查关联 REST 回显,才让用户看见自己绑到哪个库)。
META_REPO_OWNER = "owner"
META_REPO_NAME = "repo"
META_REPO_REF = "ref"
PUBLIC_METADATA_FIELDS = frozenset((
    "base_url", "account", "profile", META_DEFAULT,
    META_REPO_OWNER, META_REPO_NAME, META_REPO_REF,
))

# The auth file each CLI reads inside its isolated home, and the env var that
# relocates that home. Structure (verified against real installs):
#   claude: <CLAUDE_CONFIG_DIR>/.credentials.json = {"claudeAiOauth": {...}}
#   codex:  <CODEX_HOME>/auth.json = {"auth_mode", "tokens": {...}, ...}
# Both are auto-refreshing OAuth blobs, so a binding writes the whole file
# (from the CLI's own login, or a pasted credential file) — never a bare token.
CLI_HOME_ENV = {
    PROVIDER_CLAUDE_CLI: "CLAUDE_CONFIG_DIR",
    PROVIDER_CODEX_CLI: "CODEX_HOME",
}
CLI_HOME_AUTH_FILE = {
    PROVIDER_CLAUDE_CLI: ".credentials.json",
    PROVIDER_CODEX_CLI: "auth.json",
}


def ai_homes_dir() -> Path:
    """Root under which every user's isolated CLI config dirs live."""
    return default_system_auth_dir() / "ai-homes"


def _fs_safe_owner(owner_user_id: str) -> str:
    """Filesystem-safe, collision-free slug for a ZE user id.

    ``feishu:ou_x`` etc. contain chars we don't want in a path; sanitize to a
    readable prefix and append a short hash so distinct ids never collide onto
    the same directory (``a:b`` vs ``a_b``).
    """
    uid = _clean_owner(owner_user_id)
    prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", uid)[:40]
    digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{digest}"


def provision_cli_home(owner_user_id: str, provider: str, credential_id: str) -> Path:
    """Create (idempotently) the per-user isolated config dir for a CLI account.

    Returns the absolute path to store on ``metadata[META_HOME_DIR]``. Dir perms
    are 0700 so only the running unix user can traverse it (at-rest confidentiality
    on a shared host is bounded by that single unix user — see credentials SPEC).
    """
    prov = _clean_provider(provider)
    if prov not in CLI_PROVIDERS:
        raise ZeError(
            "validation_failed",
            "config-dir provisioning only applies to CLI providers",
            context={"provider": prov, "allowed": list(CLI_PROVIDERS)},
        )
    cid = _clean_credential_id(credential_id)
    home = ai_homes_dir() / _fs_safe_owner(owner_user_id) / prov / cid
    home.mkdir(parents=True, exist_ok=True)
    for d in (ai_homes_dir(), home.parent.parent, home.parent, home):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return home.resolve()


def write_cli_auth_file(home: Path, provider: str, auth_file_content: str) -> Path:
    """Write a pasted credential-file blob into the isolated home (0600).

    ``auth_file_content`` must be the full JSON the CLI expects (a whole
    ``.credentials.json`` / ``auth.json``, preserving refresh tokens) — not a
    bare token. Validated as JSON to fail fast on a wrong paste.
    """
    prov = _clean_provider(provider)
    filename = CLI_HOME_AUTH_FILE.get(prov)
    if not filename:
        raise ZeError("validation_failed", "provider has no CLI auth file",
                      context={"provider": prov})
    try:
        def _reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON number: {value}")

        parsed = json.loads(
            auth_file_content,
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object_pairs,
        )
    except (json.JSONDecodeError, TypeError) as e:
        raise ZeError("validation_failed",
                      "credential file content is not valid JSON",
                      context={}) from e
    except ValueError as e:
        raise ZeError("validation_failed",
                      "credential file content is not strict JSON",
                      context={}) from e
    if not isinstance(parsed, dict):
        raise ZeError("validation_failed", "credential file must be a JSON object",
                      context={})
    home = Path(home).expanduser().resolve()
    target = home / filename
    payload = json.dumps(
        parsed, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False,
    ) + "\n"
    _atomic_replace_text(
        target,
        payload,
        mode=0o600,
        error_prefix="credential_cli_auth",
        subject="CLI credential file",
    )
    return target


def cli_home_env(provider: str, home_dir: str | Path) -> dict[str, str]:
    """Map a provider + isolated home dir to the env var that relocates it."""
    prov = _clean_provider(provider)
    var = CLI_HOME_ENV.get(prov)
    if not var:
        return {}
    return {var: str(Path(home_dir).expanduser().resolve())}


def remove_cli_home(owner_user_id: str, provider: str, credential_id: str,
                    home_dir: str | Path | None = None) -> None:
    """Delete ONLY this credential's isolated CLI home subdir on revocation.

    Removing the registry record alone would leave the OAuth blob (with its
    refresh token) on disk — revocation would be half done. This removes the
    ``.../<owner>/<provider>/<cred_id>`` leaf, guarding hard against ever
    touching the owner root or the provider dir. Tolerant of an already-absent
    dir (``ignore_errors``).
    """
    prov = _clean_provider(provider)
    if prov not in CLI_PROVIDERS:
        return
    cid = _clean_credential_id(credential_id)
    owner_root = (ai_homes_dir() / _fs_safe_owner(owner_user_id)).resolve()
    if home_dir:
        try:
            target = Path(home_dir).expanduser().resolve()
        except (OSError, ValueError):
            return
    else:
        target = (owner_root / prov / cid).resolve()
    # Never rmtree the owner root, the provider dir, or anything outside this
    # owner's ai-home: the target must be the per-credential leaf named ``cid``.
    if target.name != cid or target == owner_root or owner_root not in target.parents:
        return
    shutil.rmtree(target, ignore_errors=True)


_THREAD_CTX = threading.local()


def default_credentials_path() -> Path:
    return default_system_auth_dir() / "credentials.json"


def default_credential_key_path() -> Path:
    return default_system_auth_dir() / "credentials.key"


def _clean_provider(provider: Any) -> str:
    p = str(provider or "").strip().lower()
    if p not in PROVIDERS:
        raise ZeError(
            "validation_failed",
            "unsupported credential provider",
            context={"provider": p, "allowed": list(PROVIDERS)},
        )
    return p


def _clean_status(status: Any) -> str:
    s = str(status or "").strip().lower()
    return s if s in STATUSES else STATUS_ACTIVE


def _clean_owner(user_id: Any) -> str:
    uid = str(user_id or "").strip()
    if not uid or uid.startswith("__demo"):
        raise ZeError("auth_required", "credential binding requires a signed-in ZE user")
    if any(c in uid for c in ("\x00", "\n", "\r", "\t")):
        raise ZeError("validation_failed", "invalid credential owner")
    return uid


def _clean_credential_id(value: Any = "") -> str:
    cid = str(value or "").strip()
    if not cid:
        return "cred_" + secrets.token_hex(8)
    if not cid.startswith("cred_") or len(cid) > 48:
        raise ZeError("validation_failed", "invalid credential id")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if any(c not in allowed for c in cid):
        raise ZeError("validation_failed", "invalid credential id")
    return cid


def _clean_label(label: Any, provider: str) -> str:
    text = str(label or "").strip()
    if not text:
        text = provider.replace("_", " ")
    if len(text) > 80:
        raise ZeError("validation_failed", "credential label too long")
    return text


def _clean_metadata(metadata: Any) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ZeError("validation_failed", "credential metadata must be an object")
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        k = str(key or "").strip()
        if not k or len(k) > 48:
            raise ZeError("validation_failed", "invalid credential metadata key")
        if any(c in k for c in ("\x00", "\n", "\r", "\t")):
            raise ZeError("validation_failed", "invalid credential metadata key")
        if isinstance(value, float) and not math.isfinite(value):
            raise ZeError("validation_failed", "credential metadata number must be finite")
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[k] = value
        else:
            out[k] = str(value)
    return out


# 读侧迁移词表(3.56.0):provider 的**历史默认快照**——存量绑定若恰等于某个历史默认
# (= 用户从未定制过),读出时升级为当前 DEFAULT_CAPABILITIES,无需重绑;
# 显式定制过的集合(如加过 custom-cap / 主动收窄)原样尊重,绝不并集覆盖。
_LEGACY_DEFAULT_CAPABILITIES: dict[str, tuple[frozenset, ...]] = {
    PROVIDER_CODEX_CLI: (frozenset({"llm", "agent"}),),   # 3.56 前默认(缺 image)
}


def _clean_capabilities(provider: str, raw: Any) -> tuple[str, ...]:
    values = raw if isinstance(raw, (list, tuple)) else DEFAULT_CAPABILITIES.get(provider, ())
    out: list[str] = []
    for item in values:
        cap = str(item or "").strip().lower()
        if cap and cap.replace("_", "").replace("-", "").isalnum() and cap not in out:
            out.append(cap)
    if frozenset(out) in _LEGACY_DEFAULT_CAPABILITIES.get(provider, ()):
        return tuple(DEFAULT_CAPABILITIES.get(provider, ()))
    return tuple(out or DEFAULT_CAPABILITIES.get(provider, ()))


def _write_stream(stream: Any, payload: str) -> None:
    """Fault-injection seam for a complete credential-store write."""
    stream.write(payload)


def _flush_stream(stream: Any) -> None:
    """Fault-injection seam between userspace buffering and fsync."""
    stream.flush()


def _fsync_stream(stream: Any) -> None:
    """Fault-injection seam proving the temporary file is durable pre-replace."""
    os.fsync(stream.fileno())


def _fsync_parent(path: Path) -> bool:
    """Fsync the rename directory; return False when the platform cannot do it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(str(path.parent), flags)
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


def _ensure_private_regular_file(path: Path, *, code: str, subject: str) -> None:
    """Reject symlinks/non-files and tighten an existing secret-bearing file to 0600."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EPERM, "not a regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.chmod(path, 0o600, follow_symlinks=False)
            fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError as exc:
        raise ZeError(
            code,
            f"{subject} permissions are unsafe or cannot be tightened",
            context={"path": str(path), "stage": "permissions"},
        ) from exc


def _atomic_replace_text(
    path: Path,
    payload: str,
    *,
    mode: int = 0o600,
    error_prefix: str = "credential_store",
    subject: str = "credential store",
) -> bool:
    """Write one complete JSON value through a unique temp and atomic replace.

    Pre-replace errors have the stable ``credential_store_write_failed`` code.
    A real directory-fsync error happens after the complete new file is visible,
    so it is reported separately as ``credential_store_durability_unknown``.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 - stabilize all ordinary I/O failures
        raise ZeError(
            f"{error_prefix}_write_failed",
            f"{subject} directory cannot be created: {path.parent}",
            context={"stage": "mkdir", "path": str(path), "replaced": False},
        ) from exc

    fd: int | None = None
    tmp_path: Path | None = None
    replaced = False
    stage = "temp_create"
    try:
        fd, raw_tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
        )
        tmp_path = Path(raw_tmp)
        try:
            os.fchmod(fd, mode)
        except OSError:
            # mkstemp is already 0600; this only matters for a stricter caller.
            pass
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = None
        with stream:
            stage = "write"
            _write_stream(stream, payload)
            stage = "flush"
            _flush_stream(stream)
            stage = "file_fsync"
            _fsync_stream(stream)

        stage = "replace"
        os.replace(tmp_path, path)
        replaced = True
        tmp_path = None

        stage = "directory_fsync"
        return _fsync_parent(path)
    except Exception as exc:  # noqa: BLE001 - stabilize all ordinary I/O failures
        context = {"stage": stage, "path": str(path), "replaced": replaced}
        if replaced:
            raise ZeError(
                f"{error_prefix}_durability_unknown",
                f"{subject} was replaced but directory fsync failed: {path}",
                context=context,
            ) from exc
        raise ZeError(
            f"{error_prefix}_write_failed",
            f"{subject} write failed at {stage}: {path}",
            context=context,
        ) from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_create_key(path: Path, key: bytes) -> None:
    """Durably create the immutable host key without a fixed-temp race.

    The key is a separate prepare-only aggregate from ``credentials.json``.  It
    is never rotated or replaced here once present; see the leaf SPEC for the
    deliberately explicit remaining multi-file boundary.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        raise ZeError(
            "credential_key_write_failed",
            f"credential key directory cannot be created: {path.parent}",
            context={"stage": "mkdir", "path": str(path), "replaced": False},
        ) from exc
    fd: int | None = None
    tmp_path: Path | None = None
    replaced = False
    stage = "temp_create"
    try:
        fd, raw_tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
        )
        tmp_path = Path(raw_tmp)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        stage = "write"
        view = memoryview(key)
        offset = 0
        while offset < len(view):
            written = os.write(fd, view[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "short credential key write")
            offset += written
        stage = "file_fsync"
        os.fsync(fd)
        os.close(fd)
        fd = None
        stage = "replace"
        os.replace(tmp_path, path)
        replaced = True
        tmp_path = None
        stage = "directory_fsync"
        _fsync_parent(path)
    except Exception as exc:  # noqa: BLE001
        context = {"stage": stage, "path": str(path), "replaced": replaced}
        code = "credential_key_durability_unknown" if replaced else "credential_key_write_failed"
        raise ZeError(code, f"credential key write failed at {stage}: {path}", context=context) from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


_KEYFILE_COLOCATION_WARNED: set[str] = set()


def _warn_if_key_colocated(key_path: Path) -> None:
    """Warn (once per path) when the master key file lives inside the auth dir.

    Co-locating the key with the sealed ``credentials.json`` means one directory
    read yields both ciphertext and key, so at-rest confidentiality collapses to
    filesystem permissions alone.  Deployments should move the key out or set
    ``ZE_CREDENTIAL_MASTER_KEY``.  We do NOT hard-refuse: amber and other live
    hosts already keep the key here, and refusing would take them down.
    """
    try:
        auth_dir = default_system_auth_dir().resolve()
        resolved = key_path.expanduser().resolve()
    except OSError:
        return
    if resolved.parent != auth_dir and auth_dir not in resolved.parents:
        return
    marker = str(resolved)
    if marker in _KEYFILE_COLOCATION_WARNED:
        return
    _KEYFILE_COLOCATION_WARNED.add(marker)
    _LOG.warning(
        "credential master key file is stored inside the ZE auth directory (%s); "
        "for at-rest defense move it outside or set ZE_CREDENTIAL_MASTER_KEY",
        resolved,
    )


def _load_master_key(path: Path | None = None, *, create: bool = True) -> bytes:
    raw = os.environ.get("ZE_CREDENTIAL_MASTER_KEY", "").strip()
    if raw:
        try:
            decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            if len(decoded) >= CREDENTIAL_KEY_BYTES:
                return decoded[:CREDENTIAL_KEY_BYTES]
        except Exception:
            pass
        return hashlib.sha256(raw.encode("utf-8")).digest()

    key_path = Path(path or default_credential_key_path())
    _warn_if_key_colocated(key_path)
    with _exclusive_file_lock(key_path):
        if key_path.exists():
            _ensure_private_regular_file(
                key_path,
                code="credential_key_permissions_invalid",
                subject="credential key",
            )
            try:
                existing = key_path.read_bytes()
            except OSError as exc:
                raise ZeError(
                    "credential_key_unavailable",
                    f"credential key cannot be read: {key_path}",
                    context={"path": str(key_path), "stage": "read"},
                ) from exc
            if len(existing) < CREDENTIAL_KEY_BYTES:
                raise ZeError(
                    "credential_key_invalid",
                    f"credential key has invalid length: {key_path}",
                    context={"path": str(key_path), "stage": "validate"},
                )
            # The previous 1.0 reader explicitly accepted a longer host key and
            # used its first 32 bytes.  Preserve that on-disk compatibility;
            # keys created by this implementation remain exactly 32 bytes.
            return existing[:CREDENTIAL_KEY_BYTES]
        if not create:
            raise ZeError(
                "credential_key_unavailable",
                f"credential key is missing: {key_path}",
                context={"path": str(key_path), "stage": "missing"},
            )
        key = secrets.token_bytes(CREDENTIAL_KEY_BYTES)
        _atomic_create_key(key_path, key)
        return key


def _keystream(key: bytes, nonce: bytes, size: int) -> bytes:
    chunks: list[bytes] = []
    counter = 0
    while sum(len(c) for c in chunks) < size:
        counter_bytes = counter.to_bytes(4, "big")
        chunks.append(hmac.new(key, nonce + counter_bytes, hashlib.sha256).digest())
        counter += 1
    return b"".join(chunks)[:size]


def _aad(owner: str, credential_id: str, provider: str) -> bytes:
    """Additional authenticated data binding a v2 ciphertext to its record.

    The GCM tag covers this, so a sealed blob copied onto a different record
    (other owner/id/provider) fails to open — a stolen blob cannot be re-homed
    to escalate to another user's account.
    """
    return f"{owner}|{credential_id}|{provider}".encode("utf-8")


def _aead_subkey(root: bytes) -> bytes:
    """Derive the AES-GCM key from the host master key with HKDF domain info.

    Domain separation fixes the sealed-v1 weakness of reusing one raw key for
    both the keystream and the MAC.
    """
    hkdf = _HKDF(
        algorithm=_crypto_hashes.SHA256(),
        length=CREDENTIAL_KEY_BYTES,
        salt=None,
        info=CREDENTIAL_AEAD_INFO,
    )
    return hkdf.derive(root)


def _aead_key_id(root: bytes) -> str:
    """Short, non-secret fingerprint of the master key for a clear-error match."""
    return hashlib.sha256(root).hexdigest()[:8]


def _decode_plain(plain: bytes) -> str:
    try:
        value = plain.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ZeError("credential_unavailable", "credential secret is corrupted") from exc
    if not value:
        raise ZeError("credential_unavailable", "credential secret is empty")
    return value


def _seal_v1(secret: str, key: bytes) -> dict[str, Any]:
    nonce = secrets.token_bytes(CREDENTIAL_NONCE_BYTES)
    plain = secret.encode("utf-8")
    stream = _keystream(key, nonce, len(plain))
    cipher = bytes(a ^ b for a, b in zip(plain, stream))
    mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    return {
        "schema": CREDENTIAL_SECRET_SCHEMA_VERSION,
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(cipher).decode("ascii"),
        "mac": base64.urlsafe_b64encode(mac).decode("ascii"),
    }


def _seal_v2(
    secret: str,
    root: bytes,
    *,
    owner: str,
    credential_id: str,
    provider: str,
) -> dict[str, Any]:
    subkey = _aead_subkey(root)
    nonce = secrets.token_bytes(CREDENTIAL_AEAD_NONCE_BYTES)
    aad = _aad(owner, credential_id, provider)
    cipher = _AESGCM(subkey).encrypt(nonce, secret.encode("utf-8"), aad)  # ct || tag
    return {
        "schema": CREDENTIAL_SECRET_SCHEMA_V2,
        "alg": "AESGCM",
        "key_id": _aead_key_id(root),
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(cipher).decode("ascii"),
        "aad_ver": CREDENTIAL_AAD_VERSION,
    }


def _seal(
    secret: str,
    *,
    key_path: Path | None = None,
    owner: str = "",
    credential_id: str = "",
    provider: str = "",
) -> dict[str, Any]:
    """Seal a secret at rest.

    Writes sealed-v2 (AES-GCM) when the AEAD backend is available, otherwise
    falls back to sealed-v1 so no-crypto environments stay fully functional.
    The record identity (owner/id/provider) becomes the v2 AAD; it is ignored by
    v1.
    """
    root = _load_master_key(key_path)
    if _HAS_AEAD:
        return _seal_v2(
            secret, root, owner=owner, credential_id=credential_id, provider=provider,
        )
    return _seal_v1(secret, root)


def _open_v1(blob: dict[str, Any], key: bytes) -> str:
    try:
        if set(blob) != {"schema", "nonce", "ciphertext", "mac"}:
            raise ValueError("sealed secret fields do not match schema")
        nonce = base64.b64decode(blob["nonce"], altchars=b"-_", validate=True)
        cipher = base64.b64decode(blob["ciphertext"], altchars=b"-_", validate=True)
        mac = base64.b64decode(blob["mac"], altchars=b"-_", validate=True)
        if len(nonce) != CREDENTIAL_NONCE_BYTES or len(mac) != hashlib.sha256().digest_size:
            raise ValueError("sealed secret nonce/mac length is invalid")
        if not cipher:
            raise ValueError("sealed secret ciphertext is empty")
    except Exception as exc:
        raise ZeError("credential_unavailable", "credential secret is corrupted") from exc
    expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ZeError("credential_unavailable", "credential secret cannot be opened")
    stream = _keystream(key, nonce, len(cipher))
    plain = bytes(a ^ b for a, b in zip(cipher, stream))
    return _decode_plain(plain)


def _open_v2(
    blob: dict[str, Any],
    root: bytes,
    *,
    owner: str,
    credential_id: str,
    provider: str,
) -> str:
    try:
        if set(blob) != {"schema", "alg", "key_id", "nonce", "ciphertext", "aad_ver"}:
            raise ValueError("sealed-v2 fields do not match schema")
        if blob.get("alg") != "AESGCM" or blob.get("aad_ver") != CREDENTIAL_AAD_VERSION:
            raise ValueError("sealed-v2 alg/aad version is unsupported")
        key_id = blob.get("key_id")
        if not isinstance(key_id, str):
            raise ValueError("sealed-v2 key_id is invalid")
        nonce = base64.b64decode(blob["nonce"], altchars=b"-_", validate=True)
        cipher = base64.b64decode(blob["ciphertext"], altchars=b"-_", validate=True)
        if len(nonce) != CREDENTIAL_AEAD_NONCE_BYTES:
            raise ValueError("sealed-v2 nonce length is invalid")
        if len(cipher) <= 16:  # must carry the 128-bit GCM tag plus ≥1 plaintext byte
            raise ValueError("sealed-v2 ciphertext is too short")
    except Exception as exc:
        raise ZeError("credential_unavailable", "credential secret is corrupted") from exc
    if not hmac.compare_digest(key_id, _aead_key_id(root)):
        raise ZeError(
            "credential_unavailable",
            "credential secret was sealed with a different master key",
        )
    subkey = _aead_subkey(root)
    aad = _aad(owner, credential_id, provider)
    try:
        plain = _AESGCM(subkey).decrypt(nonce, cipher, aad)
    except Exception as exc:  # noqa: BLE001 - InvalidTag / wrong AAD / wrong key
        raise ZeError("credential_unavailable", "credential secret cannot be opened") from exc
    return _decode_plain(plain)


def _open_sealed(
    blob: dict[str, Any],
    *,
    key_path: Path | None = None,
    owner: str = "",
    credential_id: str = "",
    provider: str = "",
) -> str:
    if not isinstance(blob, dict):
        raise ZeError("credential_unavailable", "credential secret is not sealed")
    schema = blob.get("schema")
    if schema == CREDENTIAL_SECRET_SCHEMA_VERSION:
        root = _load_master_key(key_path, create=False)
        return _open_v1(blob, root)
    if schema == CREDENTIAL_SECRET_SCHEMA_V2:
        if not _HAS_AEAD:
            # Fail closed: a v2 blob can only be opened with the AEAD backend.
            # Never silently fall back to plaintext or any weaker path.
            raise ZeError(
                "credential_crypto_unavailable",
                "credential secret requires the cryptography AEAD backend "
                "(sealed-v2) which is not installed in this runtime",
            )
        root = _load_master_key(key_path, create=False)
        return _open_v2(
            blob, root, owner=owner, credential_id=credential_id, provider=provider,
        )
    raise ZeError("credential_unavailable", "credential secret is not sealed")


def _maybe_migrate_secret_v2(
    secrets_map: dict[str, dict[str, Any]],
    rec: "CredentialRecord",
    *,
    key_path: Path | None,
) -> None:
    """Lazily re-seal a still-v1 secret to v2 during an upsert. Zero-loss.

    Only runs when the AEAD backend is present.  If the v1 blob cannot be opened
    (missing/rotated key), the v1 blob is left untouched — the secret is never
    dropped or overwritten (fail-closed).  The record's ``last4`` is unchanged,
    so ``_validate_candidate`` still passes after the re-seal.
    """
    if not _HAS_AEAD or not rec.secret_ref:
        return
    blob = secrets_map.get(rec.secret_ref)
    if not isinstance(blob, dict) or blob.get("schema") != CREDENTIAL_SECRET_SCHEMA_VERSION:
        return  # already v2 (or unknown schema) — nothing to migrate
    try:
        plain = _open_sealed(
            blob, key_path=key_path,
            owner=rec.owner_user_id, credential_id=rec.id, provider=rec.provider,
        )
    except ZeError:
        return  # cannot open v1 → keep it; never lose the secret
    secrets_map[rec.secret_ref] = _seal_v2(
        plain, _load_master_key(key_path),
        owner=rec.owner_user_id, credential_id=rec.id, provider=rec.provider,
    )


@dataclass
class CredentialRecord:
    id: str
    owner_user_id: str
    provider: str
    label: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    status: str = STATUS_ACTIVE
    metadata: dict[str, Any] = field(default_factory=dict)
    secret_ref: str = ""
    last4: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_verified_at: float = 0.0
    last_used_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["capabilities"] = list(self.capabilities)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CredentialRecord":
        return cls(
            id=str(d["id"]),
            owner_user_id=str(d["owner_user_id"]),
            provider=_clean_provider(d.get("provider")),
            label=str(d.get("label") or ""),
            capabilities=tuple(str(x) for x in (d.get("capabilities") or ())),
            status=_clean_status(d.get("status")),
            metadata=dict(d.get("metadata") or {}),
            secret_ref=str(d.get("secret_ref") or ""),
            last4=str(d.get("last4") or ""),
            created_at=float(d.get("created_at", time.time())),
            updated_at=float(d.get("updated_at", 0.0) or time.time()),
            last_verified_at=float(d.get("last_verified_at", 0.0) or 0.0),
            last_used_at=float(d.get("last_used_at", 0.0) or 0.0),
        )

    def public_dict(self) -> dict[str, Any]:
        d = self.to_dict()
        d.pop("owner_user_id", None)
        d.pop("secret_ref", None)
        # home_dir is an execution-side locator, not account metadata.  Returning
        # it reveals the server's filesystem layout and gives the browser a field
        # it must never round-trip as user input.
        metadata = dict(d.get("metadata") or {})
        d["metadata"] = {
            key: metadata[key] for key in PUBLIC_METADATA_FIELDS if key in metadata
        }
        d["has_secret"] = bool(self.secret_ref)
        return d


_RECORD_FIELDS = frozenset(CredentialRecord.__dataclass_fields__)
_STORE_FIELDS = frozenset(("schema_version", "credentials", "secrets"))


def _store_corrupt(path: Path, stage: str, message: str, **context: Any) -> ZeError:
    return ZeError(
        "credential_store_corrupt",
        message,
        context={"path": str(path), "stage": stage, **context},
    )


def _strict_timestamp(value: Any, *, field_name: str, path: Path, record_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _store_corrupt(
            path, "record_validate", "credential record timestamp is not numeric",
            credential_id=record_id, field=field_name,
        )
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise _store_corrupt(
            path, "record_validate", "credential record timestamp is invalid",
            credential_id=record_id, field=field_name,
        )
    return result


def _strict_record(item: Any, *, path: Path, index: int) -> CredentialRecord:
    if not isinstance(item, dict):
        raise _store_corrupt(
            path, "record_validate", "credential record is not an object", record_index=index,
        )
    if set(item) != _RECORD_FIELDS:
        raise _store_corrupt(
            path, "record_validate", "credential record fields do not match schema",
            record_index=index,
            missing=sorted(_RECORD_FIELDS - set(item)),
            extra=sorted(set(item) - _RECORD_FIELDS),
        )
    raw_id = item.get("id")
    if not isinstance(raw_id, str):
        raise _store_corrupt(
            path, "record_validate", "credential id is not a string", record_index=index,
        )
    try:
        credential_id = _clean_credential_id(raw_id)
    except ZeError as exc:
        raise _store_corrupt(
            path, "record_validate", "credential id is invalid", record_index=index,
        ) from exc
    if credential_id != raw_id:
        raise _store_corrupt(
            path, "record_validate", "credential id is not canonical", credential_id=raw_id,
        )

    raw_owner = item.get("owner_user_id")
    if not isinstance(raw_owner, str):
        raise _store_corrupt(
            path, "record_validate", "credential owner is not a string",
            credential_id=credential_id,
        )
    try:
        owner = _clean_owner(raw_owner)
    except ZeError as exc:
        raise _store_corrupt(
            path, "record_validate", "credential owner is invalid",
            credential_id=credential_id,
        ) from exc
    if owner != raw_owner:
        raise _store_corrupt(
            path, "record_validate", "credential owner is not canonical",
            credential_id=credential_id,
        )

    raw_provider = item.get("provider")
    if not isinstance(raw_provider, str):
        raise _store_corrupt(
            path, "record_validate", "credential provider is not a string",
            credential_id=credential_id,
        )
    try:
        provider = _clean_provider(raw_provider)
    except ZeError as exc:
        raise _store_corrupt(
            path, "record_validate", "credential provider is invalid",
            credential_id=credential_id,
        ) from exc
    if provider != raw_provider:
        raise _store_corrupt(
            path, "record_validate", "credential provider is not canonical",
            credential_id=credential_id,
        )

    label = item.get("label")
    if not isinstance(label, str) or not label or label != label.strip() or len(label) > 80:
        raise _store_corrupt(
            path, "record_validate", "credential label is invalid",
            credential_id=credential_id,
        )

    raw_caps = item.get("capabilities")
    if not isinstance(raw_caps, list) or not all(isinstance(cap, str) for cap in raw_caps):
        raise _store_corrupt(
            path, "record_validate", "credential capabilities are invalid",
            credential_id=credential_id,
        )
    capabilities = _clean_capabilities(provider, raw_caps)
    if list(capabilities) != raw_caps and \
            frozenset(raw_caps) not in _LEGACY_DEFAULT_CAPABILITIES.get(provider, ()):
        # 历史默认快照(如 codex 3.56 前的 llm+agent)是合法的升级形态:读出即迁移,
        # 不判 corrupt(2026-07-18 部署实证:严格校验会把存量 store 打死、服务拒启)。
        raise _store_corrupt(
            path, "record_validate", "credential capabilities are not canonical",
            credential_id=credential_id,
        )

    status = item.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        raise _store_corrupt(
            path, "record_validate", "credential status is invalid",
            credential_id=credential_id,
        )

    raw_metadata = item.get("metadata")
    if not isinstance(raw_metadata, dict):
        raise _store_corrupt(
            path, "record_validate", "credential metadata is not an object",
            credential_id=credential_id,
        )
    try:
        metadata = _clean_metadata(raw_metadata)
    except ZeError as exc:
        raise _store_corrupt(
            path, "record_validate", "credential metadata is invalid",
            credential_id=credential_id,
        ) from exc
    if metadata != raw_metadata:
        raise _store_corrupt(
            path, "record_validate", "credential metadata is not canonical",
            credential_id=credential_id,
        )

    secret_ref = item.get("secret_ref")
    if not isinstance(secret_ref, str) or secret_ref not in ("", f"secret_{credential_id}"):
        raise _store_corrupt(
            path, "record_validate", "credential secret reference is invalid",
            credential_id=credential_id,
        )
    last4 = item.get("last4")
    if (
        not isinstance(last4, str)
        or len(last4) > 4
        or any(char in last4 for char in ("\x00", "\n", "\r", "\t"))
        or (bool(secret_ref) != bool(last4))
    ):
        raise _store_corrupt(
            path, "record_validate", "credential last4/secret reference is inconsistent",
            credential_id=credential_id,
        )

    return CredentialRecord(
        id=credential_id,
        owner_user_id=owner,
        provider=provider,
        label=label,
        capabilities=capabilities,
        status=status,
        metadata=metadata,
        secret_ref=secret_ref,
        last4=last4,
        created_at=_strict_timestamp(
            item["created_at"], field_name="created_at", path=path, record_id=credential_id,
        ),
        updated_at=_strict_timestamp(
            item["updated_at"], field_name="updated_at", path=path, record_id=credential_id,
        ),
        last_verified_at=_strict_timestamp(
            item["last_verified_at"], field_name="last_verified_at", path=path,
            record_id=credential_id,
        ),
        last_used_at=_strict_timestamp(
            item["last_used_at"], field_name="last_used_at", path=path,
            record_id=credential_id,
        ),
    )


def _validate_candidate(
    records: dict[str, CredentialRecord],
    secrets_map: dict[str, dict[str, str]],
    *,
    path: Path,
    key_path: Path,
) -> None:
    referenced: set[str] = set()
    record_by_secret: dict[str, CredentialRecord] = {}
    for key, rec in records.items():
        if key != rec.id:
            raise _store_corrupt(
                path, "record_validate", "credential registry key/id mismatch",
                credential_id=rec.id,
            )
        # Re-validate the exact serialized representation so locally built
        # candidates cannot bypass the persisted schema contract.
        _strict_record(rec.to_dict(), path=path, index=-1)
        if rec.secret_ref:
            if rec.secret_ref in referenced:
                raise _store_corrupt(
                    path, "secret_validate", "credential secret is referenced more than once",
                    credential_id=rec.id,
                )
            referenced.add(rec.secret_ref)
            record_by_secret[rec.secret_ref] = rec

    if set(secrets_map) != referenced:
        raise _store_corrupt(
            path, "secret_validate", "credential records and sealed secrets disagree",
            missing_secrets=sorted(referenced - set(secrets_map)),
            orphan_secrets=sorted(set(secrets_map) - referenced),
        )
    for secret_ref, blob in secrets_map.items():
        if not isinstance(secret_ref, str) or not isinstance(blob, dict):
            raise _store_corrupt(
                path, "secret_validate", "credential secret entry is invalid",
                secret_ref=str(secret_ref),
            )
        owner_rec = record_by_secret[secret_ref]
        try:
            secret_value = _open_sealed(
                blob, key_path=key_path,
                owner=owner_rec.owner_user_id,
                credential_id=owner_rec.id,
                provider=owner_rec.provider,
            )
        except ZeError as exc:
            raise _store_corrupt(
                path, "secret_validate", "credential sealed secret cannot be verified",
                secret_ref=secret_ref, cause_code=exc.code,
            ) from exc
        if owner_rec.last4 != secret_value[-4:]:
            raise _store_corrupt(
                path, "secret_validate", "credential last4 does not match sealed secret",
                secret_ref=secret_ref,
            )


def _credential_payload(
    records: dict[str, CredentialRecord],
    secrets_map: dict[str, dict[str, str]],
    *,
    path: Path,
    key_path: Path,
) -> str:
    _validate_candidate(records, secrets_map, path=path, key_path=key_path)
    try:
        return json.dumps(
            {
                "schema_version": CREDENTIAL_STORE_SCHEMA_VERSION,
                "credentials": [records[key].to_dict() for key in sorted(records)],
                "secrets": {key: secrets_map[key] for key in sorted(secrets_map)},
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise ZeError(
            "credential_store_write_failed",
            f"credential candidate cannot be serialized: {path}",
            context={"path": str(path), "stage": "serialize", "replaced": False},
        ) from exc


class CredentialStore:
    """Shared ZE-system credential registry keyed by ZE user id.

    Every mutation is a single-file aggregate transaction: lock, strict reload,
    build and validate a detached candidate, fsync a unique temporary file,
    atomic replace, fsync the parent directory, then swap memory.  Returned
    records are detached copies and cannot mutate the store behind its back.
    """

    def __init__(self, path: str | Path | None = None,
                 key_path: str | Path | None = None) -> None:
        # Do not resolve the final component: doing so follows a pre-existing
        # symlink before the regular-file guard gets a chance to reject it.
        self._path = Path(path).expanduser().absolute() if path else default_credentials_path()
        self._key_path = Path(key_path).expanduser().absolute() if key_path else default_credential_key_path()
        self._records: dict[str, CredentialRecord] = {}
        self._secrets: dict[str, dict[str, str]] = {}
        self._directory_fsync_supported: bool | None = None
        with _exclusive_file_lock(self._path):
            if self._path.exists():
                _ensure_private_regular_file(
                    self._path,
                    code="credential_store_permissions_invalid",
                    subject="credential store",
                )
                records, secrets_map = self._read_candidate_locked()
                self._swap(records, secrets_map)
            else:
                self._persist_locked({}, {})

    @property
    def path(self) -> Path:
        return self._path

    def _read_candidate_locked(
        self,
    ) -> tuple[dict[str, CredentialRecord], dict[str, dict[str, str]]]:
        if not self._path.exists():
            raise ZeError(
                "credential_store_unavailable",
                f"credential store is missing: {self._path}",
                context={"path": str(self._path), "stage": "missing"},
            )
        try:
            with self._path.open("r", encoding="utf-8") as stream:
                raw = json.load(stream, object_pairs_hook=_strict_object_pairs)
        except (json.JSONDecodeError, UnicodeDecodeError, _DuplicateJsonKey) as exc:
            raise _store_corrupt(
                self._path, "decode", "credential store is not valid UTF-8 JSON",
            ) from exc
        except OSError as exc:
            raise ZeError(
                "credential_store_unavailable",
                f"credential store cannot be read: {self._path}",
                context={"path": str(self._path), "stage": "read"},
            ) from exc

        if not isinstance(raw, dict):
            raise _store_corrupt(self._path, "shape", "credential store is not an object")
        if set(raw) != _STORE_FIELDS:
            raise _store_corrupt(
                self._path, "shape", "credential store fields do not match schema",
                missing=sorted(_STORE_FIELDS - set(raw)),
                extra=sorted(set(raw) - _STORE_FIELDS),
            )
        if raw.get("schema_version") != CREDENTIAL_STORE_SCHEMA_VERSION:
            raise _store_corrupt(
                self._path, "schema", "credential store schema version is unsupported",
                schema_version=raw.get("schema_version"),
            )
        raw_records = raw.get("credentials")
        raw_secrets = raw.get("secrets")
        if not isinstance(raw_records, list):
            raise _store_corrupt(
                self._path, "shape", "credential store credentials is not a list",
            )
        if not isinstance(raw_secrets, dict):
            raise _store_corrupt(
                self._path, "shape", "credential store secrets is not an object",
            )

        records: dict[str, CredentialRecord] = {}
        for index, item in enumerate(raw_records):
            rec = _strict_record(item, path=self._path, index=index)
            if rec.id in records:
                raise _store_corrupt(
                    self._path, "record_validate", "duplicate credential id",
                    credential_id=rec.id,
                )
            records[rec.id] = rec
        secrets_map: dict[str, dict[str, str]] = {}
        for secret_ref, blob in raw_secrets.items():
            if not isinstance(secret_ref, str) or not isinstance(blob, dict):
                raise _store_corrupt(
                    self._path, "secret_validate", "credential secret entry is invalid",
                    secret_ref=str(secret_ref),
                )
            secrets_map[secret_ref] = copy.deepcopy(blob)
        _validate_candidate(
            records, secrets_map, path=self._path, key_path=self._key_path,
        )
        return records, secrets_map

    def _swap(
        self,
        records: dict[str, CredentialRecord],
        secrets_map: dict[str, dict[str, str]],
    ) -> None:
        self._records = records
        self._secrets = secrets_map

    def _persist_locked(
        self,
        records: dict[str, CredentialRecord],
        secrets_map: dict[str, dict[str, str]],
    ) -> None:
        payload = _credential_payload(
            records, secrets_map, path=self._path, key_path=self._key_path,
        )
        try:
            supported = _atomic_replace_text(self._path, payload, mode=0o600)
        except ZeError as exc:
            if exc.code == "credential_store_durability_unknown":
                # replace already made the complete candidate visible.  Keeping
                # stale memory would create a worse split-brain than reporting
                # the honest durability uncertainty.
                self._swap(records, secrets_map)
                self._directory_fsync_supported = None
            raise
        self._swap(records, secrets_map)
        self._directory_fsync_supported = supported

    @staticmethod
    def _copy_record(rec: CredentialRecord) -> CredentialRecord:
        return copy.deepcopy(rec)

    def _refresh_for_read_locked(
        self,
    ) -> tuple[dict[str, CredentialRecord], dict[str, dict[str, str]]]:
        records, secrets_map = self._read_candidate_locked()
        self._swap(records, secrets_map)
        return records, secrets_map

    def list_for_user(self, owner_user_id: str) -> list[CredentialRecord]:
        owner = _clean_owner(owner_user_id)
        with _exclusive_file_lock(self._path):
            records, _ = self._refresh_for_read_locked()
            rows = [self._copy_record(r) for r in records.values() if r.owner_user_id == owner]
            return sorted(rows, key=lambda r: (r.provider, r.label, r.id))

    def get_for_user(self, owner_user_id: str, credential_id: str) -> CredentialRecord:
        owner = _clean_owner(owner_user_id)
        cid = _clean_credential_id(credential_id)
        with _exclusive_file_lock(self._path):
            records, _ = self._refresh_for_read_locked()
            rec = records.get(cid)
            if rec is None or rec.owner_user_id != owner:
                raise ZeError("not_found", "credential not found", context={"credential_id": cid})
            return self._copy_record(rec)

    def upsert_for_user(
        self,
        owner_user_id: str,
        *,
        credential_id: str = "",
        provider: str,
        label: str = "",
        secret: str | None = None,
        metadata: dict[str, Any] | None = None,
        capabilities: list[str] | tuple[str, ...] | None = None,
    ) -> CredentialRecord:
        owner = _clean_owner(owner_user_id)
        prov = _clean_provider(provider)
        cid = _clean_credential_id(credential_id)
        meta = _clean_metadata(metadata)
        requested_caps = (
            _clean_capabilities(prov, capabilities)
            if capabilities is not None else None
        )
        now = time.time()
        secret_value = "" if secret is None else str(secret)
        with _exclusive_file_lock(self._path):
            records, secrets_map = self._read_candidate_locked()
            existing = records.get(cid)
            if existing is not None and existing.owner_user_id != owner:
                raise ZeError("acl_denied", "credential belongs to another user")
            if existing is not None and existing.provider != prov:
                raise ZeError(
                    "credential_provider_immutable",
                    "credential provider cannot be changed; create a new binding",
                    context={"credential_id": cid},
                )
            rec = existing or CredentialRecord(
                id=cid,
                owner_user_id=owner,
                provider=prov,
                label=_clean_label(label, prov),
                capabilities=(
                    requested_caps
                    if requested_caps is not None
                    else _clean_capabilities(prov, None)
                ),
                metadata={},
                created_at=now,
            )
            rec.label = _clean_label(label or rec.label, prov)
            if requested_caps is not None:
                rec.capabilities = requested_caps
            next_metadata = dict(rec.metadata)
            for key, value in meta.items():
                if value is None:
                    next_metadata.pop(key, None)
                else:
                    next_metadata[key] = value
            rec.metadata = next_metadata
            rec.updated_at = now
            if secret_value:
                secret_ref = rec.secret_ref or f"secret_{cid}"
                secrets_map[secret_ref] = _seal(
                    secret_value, key_path=self._key_path,
                    owner=owner, credential_id=cid, provider=prov,
                )
                rec.secret_ref = secret_ref
                rec.last4 = secret_value[-4:]
                rec.status = STATUS_ACTIVE
            elif prov in SECRET_REQUIRED_PROVIDERS and not rec.secret_ref:
                rec.status = STATUS_NEEDS_SECRET
            else:
                rec.status = STATUS_ACTIVE
            # Lazy, zero-loss migration: opportunistically re-seal a still-v1
            # secret to v2 whenever the AEAD backend is available (a freshly
            # written secret is already v2, so this only upgrades legacy blobs).
            _maybe_migrate_secret_v2(secrets_map, rec, key_path=self._key_path)
            records[cid] = rec
            self._persist_locked(records, secrets_map)
            return self._copy_record(rec)

    def delete_for_user(self, owner_user_id: str, credential_id: str) -> bool:
        owner = _clean_owner(owner_user_id)
        cid = _clean_credential_id(credential_id)
        with _exclusive_file_lock(self._path):
            records, secrets_map = self._read_candidate_locked()
            rec = records.get(cid)
            if rec is None or rec.owner_user_id != owner:
                self._swap(records, secrets_map)
                return False
            if rec.secret_ref:
                secrets_map.pop(rec.secret_ref, None)
            records.pop(cid, None)
            self._persist_locked(records, secrets_map)
            # Revocation must also wipe the on-disk OAuth blob (refresh token),
            # not just the registry record. Only the per-credential leaf dir.
            if rec.provider in CLI_PROVIDERS:
                remove_cli_home(owner, rec.provider, cid,
                                rec.metadata.get(META_HOME_DIR))
            return True

    def set_default_for_user(
        self,
        owner_user_id: str,
        credential_id: str,
    ) -> CredentialRecord:
        """Select exactly one default in one aggregate commit.

        The API previously looped over ``upsert_for_user``.  A crash halfway
        through that loop could leave zero or several defaults, and concurrent
        requests could interleave.  Default selection is an invariant of the
        user's credential aggregate, so all affected records are changed and
        persisted under the same cross-process lock and atomic replace.
        """
        owner = _clean_owner(owner_user_id)
        cid = _clean_credential_id(credential_id)
        with _exclusive_file_lock(self._path):
            records, secrets_map = self._read_candidate_locked()
            target = records.get(cid)
            if target is None or target.owner_user_id != owner:
                raise ZeError(
                    "not_found",
                    "credential not found",
                    context={"credential_id": cid},
                )
            now = time.time()
            changed = False
            for rec in records.values():
                if rec.owner_user_id != owner:
                    continue
                is_default = rec.id == cid
                if bool(rec.metadata.get(META_DEFAULT)) == is_default:
                    continue
                rec.metadata = {**rec.metadata, META_DEFAULT: is_default}
                rec.updated_at = now
                changed = True
            if changed:
                self._persist_locked(records, secrets_map)
            else:
                self._swap(records, secrets_map)
            return self._copy_record(records[cid])

    def reveal_secret(self, owner_user_id: str, credential_id: str) -> str:
        owner = _clean_owner(owner_user_id)
        cid = _clean_credential_id(credential_id)
        with _exclusive_file_lock(self._path):
            records, secrets_map = self._refresh_for_read_locked()
            rec = records.get(cid)
            if rec is None or rec.owner_user_id != owner:
                raise ZeError("not_found", "credential not found", context={"credential_id": cid})
            if not rec.secret_ref:
                raise ZeError("credential_unavailable", "credential has no secret")
            blob = secrets_map.get(rec.secret_ref)
            if not blob:
                # Strict load normally catches this; keep the public error if a
                # custom in-memory adapter ever violates the invariant.
                raise ZeError("credential_unavailable", "credential secret is missing")
            return _open_sealed(
                blob, key_path=self._key_path,
                owner=rec.owner_user_id, credential_id=rec.id, provider=rec.provider,
            )

    def find_for_user(
        self,
        owner_user_id: str,
        *,
        providers: tuple[str, ...] = (),
        capability: str = "",
    ) -> CredentialRecord | None:
        rows = self.list_for_user(owner_user_id)
        provider_set = set(providers)
        cap = capability.strip().lower()
        for rec in rows:
            if rec.status != STATUS_ACTIVE:
                continue
            if provider_set and rec.provider not in provider_set:
                continue
            if cap and cap not in rec.capabilities:
                continue
            return rec
        return None

    def mark_used(self, owner_user_id: str, credential_id: str) -> None:
        owner = _clean_owner(owner_user_id)
        cid = _clean_credential_id(credential_id)
        with _exclusive_file_lock(self._path):
            records, secrets_map = self._read_candidate_locked()
            rec = records.get(cid)
            if rec is None or rec.owner_user_id != owner:
                self._swap(records, secrets_map)
                return
            rec.last_used_at = time.time()
            records[cid] = rec
            self._persist_locked(records, secrets_map)

    def mark_needs_reauth(self, owner_user_id: str, credential_id: str) -> bool:
        """Flip a CLI binding to ``needs_reauth`` after an auth failure.

        Called by the copilot runner when a subprocess exits because its login
        no longer authenticates. Idempotent; only CLI providers are affected and
        an already-disabled record is left alone. Returns True when it flipped.
        """
        owner = _clean_owner(owner_user_id)
        cid = _clean_credential_id(credential_id)
        with _exclusive_file_lock(self._path):
            records, secrets_map = self._read_candidate_locked()
            rec = records.get(cid)
            if (rec is None or rec.owner_user_id != owner
                    or rec.provider not in CLI_PROVIDERS
                    or rec.status in (STATUS_DISABLED, STATUS_NEEDS_REAUTH)):
                self._swap(records, secrets_map)
                return False
            rec.status = STATUS_NEEDS_REAUTH
            rec.updated_at = time.time()
            records[cid] = rec
            self._persist_locked(records, secrets_map)
            return True

    def mark_verified(self, owner_user_id: str, credential_id: str) -> CredentialRecord:
        owner = _clean_owner(owner_user_id)
        cid = _clean_credential_id(credential_id)
        with _exclusive_file_lock(self._path):
            records, secrets_map = self._read_candidate_locked()
            rec = records.get(cid)
            if rec is None or rec.owner_user_id != owner:
                raise ZeError("not_found", "credential not found", context={"credential_id": cid})
            if rec.provider in SECRET_REQUIRED_PROVIDERS and not rec.secret_ref:
                # Validation must happen before the aggregate is written.  A
                # route-level check after mark_verified used to persist a
                # verification timestamp and then return an error.
                raise ZeError("requires_credential", "credential has no secret")
            rec.status = STATUS_ACTIVE
            rec.last_verified_at = time.time()
            rec.updated_at = rec.last_verified_at
            records[cid] = rec
            self._persist_locked(records, secrets_map)
            return self._copy_record(rec)


@dataclass
class CredentialContext:
    actor: Actor | None = None
    store: CredentialStore | None = None
    policy_store: Any | None = None
    tool_id: str = ""


@contextmanager
def credential_context(
    actor: Actor | None,
    store: CredentialStore | None,
    policy_store: Any | None = None,
    tool_id: str = "",
) -> Iterator[None]:
    prev = getattr(_THREAD_CTX, "credential_context", None)
    _THREAD_CTX.credential_context = CredentialContext(
        actor=actor,
        store=store,
        policy_store=policy_store,
        tool_id=tool_id,
    )
    try:
        yield
    finally:
        _THREAD_CTX.credential_context = prev


def current_credential_context() -> CredentialContext:
    ctx = getattr(_THREAD_CTX, "credential_context", None)
    return ctx if isinstance(ctx, CredentialContext) else CredentialContext()


__all__ = [
    "CredentialRecord", "CredentialStore", "CredentialContext",
    "credential_context", "current_credential_context",
    "default_credentials_path", "default_credential_key_path",
    "PROVIDER_HUPO_GATEWAY", "PROVIDER_OPENAI_API", "PROVIDER_ANTHROPIC_API",
    "PROVIDER_CURSOR_API",
    "PROVIDER_CODEX_CLI", "PROVIDER_CLAUDE_CLI", "PROVIDER_GITHUB_REPO", "PROVIDERS",
    "API_KEY_PROVIDERS", "CLI_PROVIDERS", "SECRET_REQUIRED_PROVIDERS",
    "STATUS_ACTIVE", "STATUS_NEEDS_SECRET", "STATUS_DISABLED", "STATUS_NEEDS_REAUTH",
    # Per-user CLI account isolation
    "META_HOME_DIR", "META_BIN", "META_ACCOUNT", "META_DEFAULT",
    # github_repo 关联元数据
    "META_REPO_OWNER", "META_REPO_NAME", "META_REPO_REF",
    "CLI_HOME_ENV", "CLI_HOME_AUTH_FILE",
    "ai_homes_dir", "provision_cli_home", "write_cli_auth_file", "cli_home_env",
    "remove_cli_home",
]
