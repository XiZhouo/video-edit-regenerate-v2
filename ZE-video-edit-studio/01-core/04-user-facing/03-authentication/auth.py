"""Authentication — 全局账号 + Session.

完整 SPEC 见同目录 SPEC.md.

已实装 (无跨叶子依赖):
  - User dataclass: user_id / password_hash / password_salt / created_at / roles
    + display_name / email / avatar_url / auth_provider / external_id
    + to_dict / from_dict / verify_password
  - UserStore(path=None):
      register(username, password) → User; 抛 user_exists / validation_failed
      authenticate(username, password) → User; 失败统一抛 auth_failed (防枚举)
      upsert_external_user(provider, external_id, ...) → User; 飞书/OAuth 身份写入
      get(user_id) → User | None
      exists(user_id) → bool
      list_users() → list[User]
      持久化到 ~/.claude/ze_users.json (atomic 写)
  - SessionManager(ttl_sec=7d, path=None): session 表; path 传入时持久化
      login(user) → token (32 hex bytes / 256 位熵)
      logout(token) → bool
      get_user_id(token) → str | None (顺手清当前过期 token)
      is_valid(token) → bool
      cleanup_expired() → int
      active_count() → int
  - 工具函数:
      hash_password(password, salt=None) → (hash_hex, salt_hex)
      verify_password(password, hash_hex, salt_hex) → bool  (hmac.compare_digest)
      build_actor(user, device_id, source) → Actor

设计 (见 SPEC §3-§5):
  - 内网工具; 缺省 pbkdf2-sha256(标准库,210k 迭代)+ 随机 salt; 不引第三方哈希库。
    旧存量 sha256 用户登录时惰性迁移(验证通过即重哈希落盘,不强制重设密码)。
  - auth_failed 统一码 (防枚举); 密码错跟用户不存在不区分
  - session 可持久化; 服务态用 .data/sessions.json 支撑同机免重登
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

try:  # POSIX 部署环境可用;Windows/受限环境降级为进程内锁。
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# 共享 ZeError / Actor: 从 02-entity 兄弟目录 import (v3 平铺布局)
_ENTITY_DIR = Path(__file__).resolve().parent.parent.parent / "01-data-structure" / "02-entity"
if str(_ENTITY_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTITY_DIR))

from entity import Actor, ZeError  # noqa: E402


# ==============================================================
# 配置常量
# ==============================================================

def default_system_auth_dir() -> Path:
    """ZE-system 身份权威目录。

    优先走 `ZE_SYSTEM_AUTH_DIR`;未配置时按本地 skills 布局落到
    `ZE-skill-system/ZE-skill-hub-data/auth/`。这样业务工具不再把身份数据散落到各自
    `.data/` 里,Hub 与所有 ZE 工具共享同一组 JSON 文件。
    """
    env = os.environ.get("ZE_SYSTEM_AUTH_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (
        Path.home()
        / ".claude"
        / "skills"
        / "ZE-skill-system"
        / "ZE-skill-hub-data"
        / "auth"
    )


def default_users_path() -> Path:
    return default_system_auth_dir() / "users.json"


def default_sessions_path() -> Path:
    return default_system_auth_dir() / "sessions.json"


def default_policies_path() -> Path:
    return default_system_auth_dir() / "policies.json"


def default_audit_path() -> Path:
    return default_system_auth_dir() / "auth_audit.ndjson"


DEFAULT_STORE_PATH = default_users_path()
DEFAULT_SESSION_TTL_SEC = 30 * 86400         # 30 天
STORE_SCHEMA_VERSION = "1.0.0"
SESSION_STORE_SCHEMA_VERSION = "1.0.0"
POLICY_STORE_SCHEMA_VERSION = "1.1.0"
AUTH_AUDIT_SCHEMA_VERSION = "1.0.0"

# 权限档位。none 与 ban 都不放行,区别在**能否申请**:
#   none —— 尚未授权,用户可提交申请(标准 skill 的默认档)
#   ban  —— 明确封禁,申请一律拒绝
PERMISSION_NONE = "none"
PERMISSION_READ = "read"
PERMISSION_EDIT = "edit"
PERMISSION_BAN = "ban"
PERMISSIONS = (PERMISSION_NONE, PERMISSION_READ, PERMISSION_EDIT, PERMISSION_BAN)
# 档位高低。ban 排在 none 之下,使 permission_allows 一次判定即可覆盖两种拒绝。
_PERMISSION_RANK = {
    PERMISSION_BAN: -1,
    PERMISSION_NONE: 0,
    PERMISSION_READ: 1,
    PERMISSION_EDIT: 2,
}

ACCESS_REQUEST_PENDING = "pending"
ACCESS_REQUEST_APPROVED = "approved"
ACCESS_REQUEST_REJECTED = "rejected"
ACCESS_REQUEST_STATES = (
    ACCESS_REQUEST_PENDING, ACCESS_REQUEST_APPROVED, ACCESS_REQUEST_REJECTED)
MODEL_SCOPE_LLM = "llm"
MODEL_SCOPE_IMAGE = "image"
MODEL_SCOPE_VIDEO = "video"
MODEL_SCOPE_VOICE = "voice"
MODEL_SCOPE_TTS = "tts"
MODEL_SCOPE_STT = "stt"
MODEL_SCOPE_EMBEDDING = "embedding"
MODEL_SCOPES = (
    MODEL_SCOPE_LLM,
    MODEL_SCOPE_IMAGE,
    MODEL_SCOPE_VIDEO,
    MODEL_SCOPE_VOICE,
    MODEL_SCOPE_TTS,
    MODEL_SCOPE_STT,
    MODEL_SCOPE_EMBEDDING,
)
USER_STATUS_ACTIVE = "active"
USER_STATUS_DISABLED = "disabled"
USER_STATUSES = (USER_STATUS_ACTIVE, USER_STATUS_DISABLED)

# 登录入口是部署级选择，不写进项目数据，也不由浏览器自行判断。默认保持现有
# “飞书优先、兼容本地账号”行为；独立团队部署显式切 minimal-local 即可完全不依赖飞书。
AUTH_MODE_MINIMAL_LOCAL = "minimal-local"
AUTH_MODE_FEDERATED = "federated"
AUTH_MODE_HYBRID = "hybrid"
AUTH_MODES = (AUTH_MODE_MINIMAL_LOCAL, AUTH_MODE_FEDERATED, AUTH_MODE_HYBRID)

AUTH_METHOD_LOCAL = "local"
AUTH_METHOD_FEISHU = "feishu"
AUTH_METHODS = (AUTH_METHOD_LOCAL, AUTH_METHOD_FEISHU)

AUTH_REGISTRATION_OPEN = "open"
AUTH_REGISTRATION_CLOSED = "closed"
AUTH_REGISTRATION_POLICIES = (AUTH_REGISTRATION_OPEN, AUTH_REGISTRATION_CLOSED)

LOGIN_NAME_USERNAME = "username"
LOGIN_NAME_EMAIL = "email"
LOGIN_NAME_FORMATS = (LOGIN_NAME_USERNAME, LOGIN_NAME_EMAIL)

MIN_USERNAME_LEN = 1
MAX_USERNAME_LEN = 64
MIN_PASSWORD_LEN = 8   # 2026-07: 6→8(配合慢 KDF 抬离线破解成本;只影响新设/改密,存量不受限)

# 密码哈希算法:缺省 pbkdf2-sha256(hashlib.pbkdf2_hmac,标准库,不破「不引第三方」约束);
# legacy sha256 仅用于验证旧存量记录(User.password_algo 为空即 legacy),登录成功即惰性迁移。
LEGACY_PASSWORD_ALGO = "sha256"
PBKDF2_PASSWORD_ALGO = "pbkdf2-sha256"
PBKDF2_ITERATIONS = 210_000   # OWASP 2023 SHA-256 档下限;单次 ~几十ms,登录频度可承受

# 登录限流(进程内,纵深防御):同一提交 username 连续失败 ≥ 阈值后指数退避;成功清零。
# 按提交名记账(存在与否都记)不泄露枚举;内存态重启清零——内网工具不引 Redis/持久化。
_RL_THRESHOLD = 5
_RL_BASE_LOCK_SEC = 1.0
_RL_MAX_LOCK_SEC = 60.0
MAX_PASSWORD_LEN = 128
SALT_BYTES = 16                              # → 32 hex 字符
SESSION_TOKEN_BYTES = 32                     # → 64 hex 字符 (256 位熵)

# 用户不存在时 authenticate 仍跑一次 hash 防 timing attack;
# 用一个固定 dummy salt 保证 sha256 总会跑.
_DUMMY_SALT = "0" * (SALT_BYTES * 2)


_PROCESS_IO_LOCK = threading.RLock()


@dataclass(frozen=True)
class AuthRuntimeConfig:
    """部署侧登录入口的无密钥运行快照。

    该对象只描述浏览器可以看到的能力；飞书 App Secret、账号库路径和任何 token
    都不属于这个合同。配置来自进程环境，项目文件不能覆盖它。
    """

    mode: str
    methods: tuple[str, ...]
    preferred_method: str
    registration: str
    login_name_format: str

    @property
    def allows_local(self) -> bool:
        return AUTH_METHOD_LOCAL in self.methods

    @property
    def allows_feishu(self) -> bool:
        return AUTH_METHOD_FEISHU in self.methods

    @property
    def registration_open(self) -> bool:
        return self.registration == AUTH_REGISTRATION_OPEN and self.allows_local

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "ze.auth-runtime-config.v1",
            "mode": self.mode,
            "methods": list(self.methods),
            "preferred_method": self.preferred_method,
            "registration": (
                AUTH_REGISTRATION_OPEN
                if self.registration_open
                else AUTH_REGISTRATION_CLOSED
            ),
            "login_name_format": self.login_name_format,
            # minimal-local 当前只把邮箱当登录名；没有伪装成已完成邮件验证。
            "email_verification_required": False,
        }


def _auth_env_choice(
    environ: Mapping[str, str],
    key: str,
    *,
    allowed: tuple[str, ...],
    default: str,
) -> tuple[str, bool]:
    raw = environ.get(key)
    explicit = raw is not None and str(raw).strip() != ""
    value = str(raw).strip().lower() if explicit else default
    if value not in allowed:
        raise ZeError(
            "auth_configuration_invalid",
            "authentication configuration is invalid",
            context={"field": key, "allowed": list(allowed)},
        )
    return value, explicit


def auth_runtime_config(environ: Mapping[str, str] | None = None) -> AuthRuntimeConfig:
    """解析部署级登录入口。

    - minimal-local:仅本系统账号；邮箱形状只作登录名，不发验证邮件。
    - federated:仅飞书身份入口，不开放本地注册/登录。
    - hybrid:两者并存；默认飞书优先，保留既有本地账号兼容面。

    配错值时 fail-closed，不静默猜测另一种身份边界。
    """

    env: Mapping[str, str] = os.environ if environ is None else environ
    mode, _ = _auth_env_choice(
        env,
        "ZE_AUTH_MODE",
        allowed=AUTH_MODES,
        default=AUTH_MODE_HYBRID,
    )
    registration, _ = _auth_env_choice(
        env,
        "ZE_AUTH_REGISTRATION",
        allowed=AUTH_REGISTRATION_POLICIES,
        default=AUTH_REGISTRATION_OPEN,
    )

    if mode == AUTH_MODE_MINIMAL_LOCAL:
        methods = (AUTH_METHOD_LOCAL,)
        login_name_format = LOGIN_NAME_EMAIL
        default_preferred = AUTH_METHOD_LOCAL
    elif mode == AUTH_MODE_FEDERATED:
        methods = (AUTH_METHOD_FEISHU,)
        login_name_format = LOGIN_NAME_USERNAME
        default_preferred = AUTH_METHOD_FEISHU
    else:
        methods = (AUTH_METHOD_LOCAL, AUTH_METHOD_FEISHU)
        login_name_format = LOGIN_NAME_USERNAME
        default_preferred = AUTH_METHOD_FEISHU

    preferred, preferred_explicit = _auth_env_choice(
        env,
        "ZE_AUTH_PREFERRED_METHOD",
        allowed=AUTH_METHODS,
        default=default_preferred,
    )
    if preferred not in methods:
        # 只有显式矛盾才报错；缺省值总由当前 mode 推导。
        if preferred_explicit:
            raise ZeError(
                "auth_configuration_invalid",
                "preferred authentication method is disabled by the selected mode",
                context={"field": "ZE_AUTH_PREFERRED_METHOD", "mode": mode},
            )
        preferred = default_preferred

    if AUTH_METHOD_LOCAL not in methods:
        registration = AUTH_REGISTRATION_CLOSED

    return AuthRuntimeConfig(
        mode=mode,
        methods=methods,
        preferred_method=preferred,
        registration=registration,
        login_name_format=login_name_format,
    )


def normalize_local_login_name(value: Any, *, login_name_format: str) -> str:
    """按部署合同规范本地登录名；旧 username 模式保持逐字兼容。

    email 模式只验证“可稳定识别的邮箱形状”，不声称邮箱真实或已验证。注册和登录
    必须调用同一函数，保证大小写与首尾空白不会生成第二个账号。
    """

    if login_name_format not in LOGIN_NAME_FORMATS:
        raise ZeError(
            "auth_configuration_invalid",
            "authentication login-name format is invalid",
            context={"field": "login_name_format"},
        )
    if login_name_format == LOGIN_NAME_USERNAME:
        return value
    if not isinstance(value, str):
        raise ZeError("validation_failed", "login name must be a string", context={})

    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if len(normalized) > MAX_USERNAME_LEN or normalized.count("@") != 1:
        raise ZeError("validation_failed", "login name must be an email address", context={})
    local, domain = normalized.rsplit("@", 1)
    labels = domain.split(".")
    if (
        not local
        or len(labels) < 2
        or any(not label or label.startswith("-") or label.endswith("-") for label in labels)
        or any(not (ch.isascii() and (ch.isalnum() or ch == "-")) for ch in domain if ch != ".")
        or any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") for ch in normalized)
    ):
        raise ZeError("validation_failed", "login name must be an email address", context={})
    _validate_username(normalized)
    return normalized


@contextmanager
def _exclusive_file_lock(path: Path):
    """跨进程 JSON 读改写锁。

    原子 replace 能防半文件,但防不了两个服务同时 load→mutate→save 的 lost update。
    auth 目录由 Hub 与多工具共享,所以写策略/用户/session 前用同一个 `.lock` 文件串行化。
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _PROCESS_IO_LOCK:
        with lock_path.open("a+", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _read_json_file(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_json_file(path: Path, payload: dict, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _clean_permission(value: Any, default: str = PERMISSION_READ) -> str:
    v = str(value or "").strip().lower()
    return v if v in PERMISSIONS else default


def permission_allows(permission: Any, required: str = PERMISSION_READ) -> bool:
    """持有档位是否够用。

    唯一的放行判定入口 —— 调用方不要自己写 ``== PERMISSION_BAN``,那种写法在新增
    ``none`` 档时会把无权限用户当成放行。
    """
    have = _PERMISSION_RANK.get(_clean_permission(permission, default=PERMISSION_NONE), 0)
    need = _PERMISSION_RANK.get(_clean_permission(required, default=PERMISSION_READ), 1)
    return have >= max(need, _PERMISSION_RANK[PERMISSION_READ])


def _request_key(user_id: str, tool_id: str) -> str:
    """申请以 用户×工具 为唯一键 —— 重复提交覆盖同一行,不堆积重复申请。"""
    return f"{user_id}|{tool_id}"


def _clean_request_state(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in ACCESS_REQUEST_STATES else ACCESS_REQUEST_PENDING


def _clean_model_scope(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v not in MODEL_SCOPES:
        raise ZeError(
            "validation_failed",
            "unsupported model scope",
            context={"scope": v, "allowed": list(MODEL_SCOPES)},
        )
    return v


def _clean_user_status(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in USER_STATUSES else USER_STATUS_ACTIVE


def super_admin_users() -> set[str]:
    raw = os.environ.get("ZE_SUPER_ADMIN_USERS", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_super_admin(user_id: str) -> bool:
    return bool(user_id and user_id in super_admin_users())


# ==============================================================
# 密码哈希工具
# ==============================================================

def hash_password(
    password: str, salt: str | None = None, *, algo: str = PBKDF2_PASSWORD_ALGO,
) -> tuple[str, str]:
    """密码哈希, 返 (hash_hex, salt_hex). 缺省 pbkdf2-sha256(210k 迭代);
    algo=LEGACY_PASSWORD_ALGO 时为 sha256(salt+password), 仅供验证旧存量。

    salt 不传则随机生成 (secrets.token_hex; 强随机). 两种算法 digest 均 64 hex。
    """
    if not isinstance(password, str):
        raise ZeError(
            "validation_failed",
            f"password must be str, got {type(password).__name__}",
            context={"type": type(password).__name__},
        )
    if salt is None:
        salt = secrets.token_hex(SALT_BYTES)
    if algo == LEGACY_PASSWORD_ALGO:
        digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    else:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS,
        ).hex()
    return digest, salt


def verify_password(
    password: str, expected_hash: str, salt: str, algo: str = PBKDF2_PASSWORD_ALGO,
) -> bool:
    """hmac.compare_digest 防 timing attack. algo 缺省与 hash_password 自洽(pbkdf2);
    旧存量记录经 User.verify_password 按 password_algo 显式传 legacy."""
    if not isinstance(password, str):
        return False
    computed, _ = hash_password(password, salt, algo=algo)
    if algo == LEGACY_PASSWORD_ALGO:
        # 时间均衡:legacy 校验只花单次 sha256(~微秒),与 pbkdf2(~几十ms)相差 4 个量级,
        # 否则「存在的未迁移旧用户」在错密路径上可被计时枚举(review HIGH);补烧一次
        # pbkdf2 dummy 把 legacy 路径成本抬到与 pbkdf2 路径同级。迁移完成后自然消失。
        hash_password(password, _DUMMY_SALT)
    return hmac.compare_digest(computed, expected_hash)


# ==============================================================
# User dataclass
# ==============================================================

@dataclass
class User:
    user_id: str
    password_hash: str = ""
    password_salt: str = ""
    # 密码哈希算法标记:"" = 旧存量 sha256(legacy);新记录 = pbkdf2-sha256。登录时惰性迁移。
    password_algo: str = ""
    created_at: float = field(default_factory=time.time)
    roles: tuple[str, ...] = ("user",)
    display_name: str = ""
    email: str = ""
    avatar_url: str = ""
    auth_provider: str = "password"
    external_id: str = ""
    first_seen_at: float = field(default_factory=time.time)
    last_seen_at: float = 0.0
    last_login_at: float = 0.0
    last_login_tool: str = ""
    status: str = USER_STATUS_ACTIVE

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["roles"] = list(self.roles)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "User":
        created = float(d.get("created_at", d.get("first_seen_at", time.time())))
        first_seen = float(d.get("first_seen_at", created))
        return cls(
            user_id=d["user_id"],
            password_hash=str(d.get("password_hash") or ""),
            password_salt=str(d.get("password_salt") or ""),
            password_algo=str(d.get("password_algo") or ""),
            created_at=created,
            roles=tuple(d.get("roles") or ("user",)),
            display_name=str(d.get("display_name") or ""),
            email=str(d.get("email") or ""),
            avatar_url=str(d.get("avatar_url") or ""),
            auth_provider=str(d.get("auth_provider") or "password"),
            external_id=str(d.get("external_id") or ""),
            first_seen_at=first_seen,
            last_seen_at=float(d.get("last_seen_at", d.get("last_login_at", 0.0)) or 0.0),
            last_login_at=float(d.get("last_login_at", 0.0) or 0.0),
            last_login_tool=str(d.get("last_login_tool") or ""),
            status=_clean_user_status(d.get("status")),
        )

    def verify_password(self, password: str) -> bool:
        if not self.password_hash or not self.password_salt:
            return False
        algo = self.password_algo or LEGACY_PASSWORD_ALGO
        return verify_password(password, self.password_hash, self.password_salt, algo=algo)


# ==============================================================
# User directory — public server-side cards/search
# ==============================================================

_FEISHU_OPEN_ID_PREFIXES = ("ou_", "on_")


def user_has_channel(user: User, channel: str) -> bool:
    """Return whether a user can be addressed through a public channel."""
    ch = str(channel or "").strip().lower()
    if not ch:
        return True
    if ch == "feishu":
        return (
            user.auth_provider == "feishu"
            and bool(user.external_id)
            and user.external_id.startswith(_FEISHU_OPEN_ID_PREFIXES)
        )
    return False


def user_public_card(
    user: User,
    *,
    permission: str | None = None,
    tool_id: str | None = None,
) -> dict[str, Any]:
    """Sanitized identity card for sharing/search UI.

    This intentionally omits password hash/salt and raw provider external ids. The
    server may still use external_id internally to resolve delivery channels.
    """
    card: dict[str, Any] = {
        "id": user.user_id,
        "name": user.display_name or user.email or user.user_id,
        "email": user.email or None,
        "avatar_url": user.avatar_url or None,
        "status": user.status,
        "first_seen_at": user.first_seen_at,
        "last_seen_at": user.last_seen_at,
        "last_login_at": user.last_login_at,
        "last_login_tool": user.last_login_tool,
        "channels": {
            "feishu": user_has_channel(user, "feishu"),
        },
    }
    if permission is not None:
        card["permission"] = permission
    if tool_id is not None:
        card["tool_id"] = tool_id
    return card


def _user_search_blob(user: User) -> str:
    return " ".join((
        user.user_id,
        user.display_name,
        user.email,
        user.external_id,
        user.last_login_tool,
    )).lower()


def user_matches_query(user: User, query: str) -> bool:
    q = str(query or "").strip().lower()
    if not q:
        return True
    return q in _user_search_blob(user)


def search_users(
    users: list[User],
    *,
    query: str = "",
    limit: int = 50,
    channel: str = "",
    include_disabled: bool = False,
) -> list[User]:
    """Search active ZE users in deterministic UI-friendly order."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 50
    n = max(1, min(n, 100))
    out: list[User] = []
    ordered = sorted(
        users,
        key=lambda u: (-(u.last_login_at or u.last_seen_at or u.first_seen_at or 0), u.user_id),
    )
    for user in ordered:
        if not include_disabled and user.status == USER_STATUS_DISABLED:
            continue
        if channel and not user_has_channel(user, channel):
            continue
        if not user_matches_query(user, query):
            continue
        out.append(user)
        if len(out) >= n:
            break
    return out


def lookup_users(
    users: list[User],
    ids: list[str],
    *,
    channel: str = "",
    include_disabled: bool = False,
) -> tuple[list[User], list[str]]:
    """Lookup user ids preserving input order and returning missing ids."""
    by_id = {u.user_id: u for u in users}
    found: list[User] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw in ids[:100]:
        uid = str(raw or "").strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        user = by_id.get(uid)
        if (
            user is None
            or (not include_disabled and user.status == USER_STATUS_DISABLED)
            or (channel and not user_has_channel(user, channel))
        ):
            missing.append(uid)
            continue
        found.append(user)
    return found, missing


# ==============================================================
# UserStore — 全局用户表
# ==============================================================

def _validate_username(username: str) -> None:
    if not isinstance(username, str):
        raise ZeError(
            "validation_failed",
            f"username must be str, got {type(username).__name__}",
            context={"type": type(username).__name__},
        )
    stripped = username.strip()
    if not stripped:
        raise ZeError("validation_failed", "username cannot be empty/whitespace",
                      context={"username": username})
    # 保留前缀:框架用 "__" 前缀标识本机全权/访客伪账号(__system__ / __mcp__ / __demo_*,
    # 见 sdk/bootstrap、sdk/mcp_bridge、sdk/model 的 "__" 豁免分支)。禁真实用户注册该前缀,
    # 否则可冒充这些身份套用 per-user 凭据豁免、model scope 豁免或 document.set_acl owner-only
    # 豁免(安全:身份边界)。username = user_id,故按原始串判前缀。
    if username.startswith("__"):
        raise ZeError("validation_failed",
                      "username 不能以保留前缀 '__' 开头",
                      context={"username": username})
    if len(username) < MIN_USERNAME_LEN or len(username) > MAX_USERNAME_LEN:
        raise ZeError(
            "validation_failed",
            f"username length must be {MIN_USERNAME_LEN}-{MAX_USERNAME_LEN}, got {len(username)}",
            context={"username_length": len(username)},
        )
    # 不允许冒号 / 斜杠等可能跟其他 ID 系统冲突的字符 (保守)
    bad_chars = set(username) & set(":/\\\t\n\r")
    if bad_chars:
        raise ZeError(
            "validation_failed",
            f"username contains illegal chars: {bad_chars}",
            context={"chars": list(bad_chars)},
        )
    # 拒绝 Unicode 控制字符 / 格式控制字符 (含 \x00 / zero-width space 等)
    # Cc = control, Cf = format (e.g. ZWSP / RTL marker)
    for c in username:
        if unicodedata.category(c) in ("Cc", "Cf"):
            raise ZeError(
                "validation_failed",
                f"username contains control/format char U+{ord(c):04X}",
                context={"codepoint": ord(c)},
            )


def _validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise ZeError(
            "validation_failed",
            f"password must be str, got {type(password).__name__}",
            context={"type": type(password).__name__},
        )
    if not password.strip():
        raise ZeError("validation_failed", "password cannot be empty/whitespace",
                      context={})
    # 禁止前后空白 — 前端 UI 通常会 trim, 存了带空白的密码下次登录会失败
    if password != password.strip():
        raise ZeError(
            "validation_failed",
            "password cannot have leading/trailing whitespace",
            context={},
        )
    if len(password) < MIN_PASSWORD_LEN or len(password) > MAX_PASSWORD_LEN:
        raise ZeError(
            "validation_failed",
            f"password length must be {MIN_PASSWORD_LEN}-{MAX_PASSWORD_LEN}, got {len(password)}",
            context={"password_length": len(password)},
        )


def _validate_external_identity(provider: str, external_id: str) -> tuple[str, str]:
    if not isinstance(provider, str) or not isinstance(external_id, str):
        raise ZeError("validation_failed", "provider and external_id must be strings")
    p = provider.strip().lower()
    x = external_id.strip()
    if not p or len(p) > 32:
        raise ZeError("validation_failed", "provider cannot be empty or too long")
    if not x or len(x) > 128:
        raise ZeError("validation_failed", "external_id cannot be empty or too long")
    for c in p:
        if not (c.isascii() and (c.isalnum() or c in ("_", "-"))):
            raise ZeError("validation_failed", "provider contains illegal chars")
    for c in x:
        if c in ("/", "\\", "\x00", "\t", "\n", "\r") or unicodedata.category(c) in ("Cc", "Cf"):
            raise ZeError("validation_failed", "external_id contains illegal chars")
    return p, x


def external_user_id(provider: str, external_id: str) -> str:
    """稳定外部身份 → ZE user_id。带 provider 前缀避免跟旧 username 冲突。"""
    p, x = _validate_external_identity(provider, external_id)
    return f"{p}:{x}"


class UserStore:
    """全局用户表 (~/.claude/ze_users.json).

    register / authenticate / upsert_external_user / get / exists / list_users.
    atomic 写; 跨 skill 共享.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser().resolve() if path else default_users_path()
        self._users: dict[str, User] = {}
        # 登录限流账本(进程内): 提交的 username → (连续失败次数, 锁定截止时刻)。成功清零。
        self._login_fails: dict[str, tuple[int, float]] = {}
        with _exclusive_file_lock(self._path):
            self._load()

    @property
    def path(self) -> Path:
        return self._path

    # ---- 内部 IO ----

    def _load(self) -> None:
        if not self._path.exists():
            self._users = {}
            return
        raw = _read_json_file(self._path)
        if raw is None:
            # 损坏 / 不可读时不抛, 保持空; 业务方可手动修复 / 重置
            self._users = {}
            return
        users_list = raw.get("users", []) if isinstance(raw, dict) else []
        out: dict[str, User] = {}
        for u_dict in users_list:
            try:
                u = User.from_dict(u_dict)
                out[u.user_id] = u
            except (KeyError, TypeError, ValueError):
                # 单条损坏跳过, 不污染其他用户
                continue
        self._users = out

    def _save(self) -> None:
        payload = {
            "schema_version": STORE_SCHEMA_VERSION,
            "users": [u.to_dict() for u in self._users.values()],
        }
        _write_json_file(self._path, payload, mode=0o600)

    # ---- API ----

    def register(self, username: str, password: str) -> User:
        """注册. username 已存在抛 user_exists; 格式错抛 validation_failed."""
        _validate_username(username)
        _validate_password(password)
        with _exclusive_file_lock(self._path):
            self._load()
            if username in self._users:
                raise ZeError(
                    "user_exists",
                    f"username {username!r} already registered",
                    context={"username": username},
                )
            hash_hex, salt_hex = hash_password(password)
            now = time.time()
            user = User(
                user_id=username,
                password_hash=hash_hex,
                password_salt=salt_hex,
                password_algo=PBKDF2_PASSWORD_ALGO,
                first_seen_at=now,
                last_seen_at=now,
            )
            self._users[username] = user
            self._save()
            return user

    def authenticate(self, username: str, password: str) -> User:
        """登录. 用户不存在 / 密码错统一抛 auth_failed (防枚举).

        防 timing attack: 即使 user 不存在也跑一次 hash_password (用 dummy salt),
        让两条失败路径耗时基本一致, 无法靠响应时间区分"用户不存在 vs 密码错".
        登录限流(进程内纵深防御): 同一提交 username 连续失败 ≥ _RL_THRESHOLD 次后指数
        退避锁定(_RL_BASE_LOCK_SEC 起每次翻倍, 封顶 _RL_MAX_LOCK_SEC), 成功登录清零;
        存在与否都记账, 不泄露枚举。惰性迁移: 旧 sha256 存量验证通过后当场重哈希为
        pbkdf2 并落盘, 不强制重设密码。
        """
        # 处理非 str 输入: 仍跑一次 dummy hash 让 timing 一致
        if not isinstance(username, str) or not isinstance(password, str):
            hash_password(password if isinstance(password, str) else "x", _DUMMY_SALT)
            raise ZeError("auth_failed", "invalid credentials", context={})

        now = time.time()
        rec = self._login_fails.get(username)
        if rec and now < rec[1]:
            retry_after = max(1, int(rec[1] - now) + 1)
            raise ZeError(
                "rate_limited",
                f"too many failed attempts; retry in {retry_after}s",
                context={"retry_after_sec": retry_after},
            )

        with _exclusive_file_lock(self._path):
            self._load()
            user = self._users.get(username)
        if user is None:
            # dummy 校验: 跑跟真实场景同等开销的哈希, 防 timing
            hash_password(password, _DUMMY_SALT)
            self._register_login_fail(username, now)
            raise ZeError("auth_failed", "invalid credentials", context={})

        if not user.verify_password(password):
            self._register_login_fail(username, now)
            raise ZeError("auth_failed", "invalid credentials", context={})
        # 验证密码所有权后才查 status: 禁用用户即使密码正确也不能登。此前遗漏 —— status 只在
        # MCP _require_tool_access / HTTP 工具访问时拦,登录本身放行 → 被禁用户仍能签发有效
        # session token(仅在后续访问工具时才被挡)。密码已验证故告知 account disabled 安全
        # (非匿名枚举: 密码错在上一步已统一 invalid credentials 挡下), message 仍走 auth_failed 码。
        if user.status == USER_STATUS_DISABLED:
            raise ZeError("auth_failed", "account disabled", context={})
        self._login_fails.pop(username, None)
        if (user.password_algo or LEGACY_PASSWORD_ALGO) != PBKDF2_PASSWORD_ALGO:
            user = self._migrate_password_hash(username, password) or user
        return user

    def _register_login_fail(self, username: str, now: float) -> None:
        """失败记账 + 指数退避锁定时刻(阈值内 lock=0 只计数)。"""
        n = (self._login_fails.get(username) or (0, 0.0))[0] + 1
        lock = 0.0
        if n >= _RL_THRESHOLD:
            lock = min(_RL_MAX_LOCK_SEC, _RL_BASE_LOCK_SEC * (2 ** (n - _RL_THRESHOLD)))
        self._login_fails[username] = (n, now + lock)

    def _migrate_password_hash(self, username: str, password: str) -> User | None:
        """惰性迁移: 重锁重验后升 pbkdf2。锁内重验防丢并发更新;验不过(期间密码已改)放弃。"""
        with _exclusive_file_lock(self._path):
            self._load()
            u = self._users.get(username)
            if u is None or not u.verify_password(password):
                return None
            h, s = hash_password(password)   # 缺省即 pbkdf2
            u.password_hash, u.password_salt, u.password_algo = h, s, PBKDF2_PASSWORD_ALGO
            self._save()
            return u

    def upsert_external_user(
        self, *,
        provider: str,
        external_id: str,
        display_name: str = "",
        email: str = "",
        avatar_url: str = "",
        tool_id: str = "",
    ) -> User:
        """创建或更新外部登录用户。

        飞书 OAuth 的稳定锚点是 open_id(应用维度)。ZE 内部 user_id 写成
        `provider:external_id`,这样不会跟旧用户名密码账号碰撞;display/email/avatar
        只作展示资料,不参与身份判断。
        """
        p, x = _validate_external_identity(provider, external_id)
        uid = f"{p}:{x}"
        now = time.time()
        with _exclusive_file_lock(self._path):
            self._load()
            existing = self._users.get(uid)
            if existing is not None and existing.status == USER_STATUS_DISABLED:
                # 已存在的外部用户被禁用 → 拒绝重新登录(与 authenticate 的 password 路径同口径)。
                # 此前遗漏: existing 分支保留 status=disabled 却仍返回 user、caller(lark/sse)随即
                # session_mgr.login → 被禁外部用户仍签发有效 token(飞书 OAuth 入口,code-review CRITICAL)。
                raise ZeError("auth_failed", "account disabled", context={})
            if existing is None:
                user = User(
                    user_id=uid,
                    roles=("user",),
                    display_name=(display_name or "").strip(),
                    email=(email or "").strip(),
                    avatar_url=(avatar_url or "").strip(),
                    auth_provider=p,
                    external_id=x,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_login_at=now,
                    last_login_tool=tool_id,
                )
            else:
                user = User(
                    user_id=existing.user_id,
                    password_hash=existing.password_hash,
                    password_salt=existing.password_salt,
                    created_at=existing.created_at,
                    roles=existing.roles,
                    display_name=(display_name or existing.display_name or "").strip(),
                    email=(email or existing.email or "").strip(),
                    avatar_url=(avatar_url or existing.avatar_url or "").strip(),
                    auth_provider=p,
                    external_id=x,
                    first_seen_at=existing.first_seen_at or existing.created_at,
                    last_seen_at=now,
                    last_login_at=now,
                    last_login_tool=tool_id or existing.last_login_tool,
                    status=existing.status,
                )
            self._users[uid] = user
            self._save()
            return user

    def record_login(self, user_id: str, *, tool_id: str = "") -> User | None:
        """更新最近登录字段。本地账号兼容路径在服务端登录成功后调用。"""
        now = time.time()
        with _exclusive_file_lock(self._path):
            self._load()
            existing = self._users.get(user_id)
            if existing is None:
                return None
            user = User(
                user_id=existing.user_id,
                password_hash=existing.password_hash,
                password_salt=existing.password_salt,
                password_algo=existing.password_algo,
                created_at=existing.created_at,
                roles=existing.roles,
                display_name=existing.display_name,
                email=existing.email,
                avatar_url=existing.avatar_url,
                auth_provider=existing.auth_provider,
                external_id=existing.external_id,
                first_seen_at=existing.first_seen_at or existing.created_at,
                last_seen_at=now,
                last_login_at=now,
                last_login_tool=tool_id or existing.last_login_tool,
                status=existing.status,
            )
            self._users[user_id] = user
            self._save()
            return user

    def set_status(self, user_id: str, status: str) -> User:
        clean = _clean_user_status(status)
        with _exclusive_file_lock(self._path):
            self._load()
            existing = self._users.get(user_id)
            if existing is None:
                raise ZeError("not_found", f"user {user_id!r} not found")
            user = User(
                user_id=existing.user_id,
                password_hash=existing.password_hash,
                password_salt=existing.password_salt,
                created_at=existing.created_at,
                roles=existing.roles,
                display_name=existing.display_name,
                email=existing.email,
                avatar_url=existing.avatar_url,
                auth_provider=existing.auth_provider,
                external_id=existing.external_id,
                first_seen_at=existing.first_seen_at or existing.created_at,
                last_seen_at=existing.last_seen_at,
                last_login_at=existing.last_login_at,
                last_login_tool=existing.last_login_tool,
                status=clean,
            )
            self._users[user_id] = user
            self._save()
            return user

    def get(self, user_id: str) -> User | None:
        with _exclusive_file_lock(self._path):
            self._load()
        return self._users.get(user_id)

    def exists(self, user_id: str) -> bool:
        with _exclusive_file_lock(self._path):
            self._load()
        return user_id in self._users

    def list_users(self) -> list[User]:
        with _exclusive_file_lock(self._path):
            self._load()
        return list(self._users.values())


# ==============================================================
# SessionManager — session 表
# ==============================================================

@dataclass
class _SessionInfo:
    user_id: str
    created_at: float
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "_SessionInfo":
        return cls(
            user_id=str(d["user_id"]),
            created_at=float(d["created_at"]),
            expires_at=float(d["expires_at"]),
        )


class SessionManager:
    """Session 表; path 传入时持久化,否则保持纯内存。

    login → token; get_user_id(token) → user_id; logout(token).
    TTL 过期自动失效; get_user_id 调用时顺手清当前 token.
    """

    def __init__(self, ttl_sec: int = DEFAULT_SESSION_TTL_SEC,
                 path: str | Path | None = None) -> None:
        if not isinstance(ttl_sec, int) or ttl_sec <= 0:
            raise ZeError(
                "validation_failed",
                f"ttl_sec must be positive int, got {ttl_sec!r}",
                context={"ttl_sec": ttl_sec},
            )
        self._ttl_sec = ttl_sec
        self._path = Path(path).expanduser().resolve() if path else None
        self._sessions: dict[str, _SessionInfo] = {}
        # 解析缓存的失效键:(mtime_ns, size, inode)。见 _load 的说明。
        self._parse_key: tuple | None = None
        self._lock = threading.RLock()
        if self._path is not None:
            with _exclusive_file_lock(self._path):
                self._load()

    @property
    def path(self) -> Path | None:
        return self._path

    # ---- 内部 IO ----

    def _stat_key(self) -> tuple | None:
        """文件身份指纹:(mtime_ns, size, inode)。任一变化都说明内容可能变了。

        用 mtime **纳秒**而不是秒 —— 秒级粒度下「同一秒内先写后读」会读到陈旧解析。
        """
        try:
            st = self._path.stat()          # type: ignore[union-attr]
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _load(self) -> None:
        """把 session 表读进内存。**文件没变就不重新解析。**

        为什么要缓存:这个方法在 get_user_id 的热路径上,每次调用都全量重解析整个
        sessions.json,而且整段包在跨进程文件锁里 —— 五个台子共用一份表时是全局互斥。
        实测该表被 pytest 夹具撑到 2.98 MB / 16975 条(96.6% 是 alice/bob 这类夹具身份)后,
        单次 get_user_id 从 0.1-0.2 ms 涨到 22-30 ms(**150-200 倍**),20 并发带 cookie
        要 0.461 s 且明显串行;不带 cookie 只要 0.029 s。差距全部由存储体积造成。

        为什么缓存是安全的:**过期判定不依赖这里**。get_user_id 自己逐 token 判
        `time.time() >= info.expires_at`,所以缓存命中不会让过期 session 复活。
        缓存只跳过「读文件 + 解析 JSON」这一段,失效键取 (mtime_ns, size, inode),
        外部进程任何一次写都会改变它;而调用点持着排他文件锁,不会读到写到一半的状态。

        顺带治掉一个更糟的:原实现在过滤掉过期条目后会调 _save() ——
        **在读路径上回写整个 2.98 MB**。缓存命中时不再走到那里。
        """
        if self._path is None or not self._path.exists():
            self._sessions = {}
            self._parse_key = None
            return
        key = self._stat_key()
        if key is not None and key == self._parse_key:
            return                          # 文件没变,内存里那份就是最新的
        raw = _read_json_file(self._path)
        if raw is None:
            self._sessions = {}
            self._parse_key = None          # 读失败不缓存,下次照样重试
            return
        sessions = raw.get("sessions", {}) if isinstance(raw, dict) else {}
        if not isinstance(sessions, dict):
            self._sessions = {}
            self._parse_key = key
            return
        now = time.time()
        out: dict[str, _SessionInfo] = {}
        for token, info_dict in sessions.items():
            if not isinstance(token, str) or not isinstance(info_dict, dict):
                continue
            try:
                info = _SessionInfo.from_dict(info_dict)
            except (KeyError, TypeError, ValueError):
                continue
            if now < info.expires_at:
                out[token] = info
        self._sessions = out
        self._parse_key = key
        if len(out) != len(sessions):
            self._save()                    # _save 内部会把 _parse_key 更新成新文件的指纹

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {
            "schema_version": SESSION_STORE_SCHEMA_VERSION,
            "sessions": {token: info.to_dict() for token, info in self._sessions.items()},
        }
        _write_json_file(self._path, payload, mode=0o600)
        # 自己写的这一次不该让下一次读作废 —— 内存里那份就是刚写下去的内容。
        self._parse_key = self._stat_key()

    def login(self, user: User) -> str:
        if not isinstance(user, User):
            raise ZeError(
                "validation_failed",
                f"login expects User, got {type(user).__name__}",
                context={"type": type(user).__name__},
            )
        token = secrets.token_hex(SESSION_TOKEN_BYTES)
        now = time.time()
        with self._lock:
            if self._path is not None:
                with _exclusive_file_lock(self._path):
                    self._load()
                    self._sessions[token] = _SessionInfo(
                        user_id=user.user_id,
                        created_at=now,
                        expires_at=now + self._ttl_sec,
                    )
                    self._save()
            else:
                self._sessions[token] = _SessionInfo(
                    user_id=user.user_id,
                    created_at=now,
                    expires_at=now + self._ttl_sec,
                )
        return token

    def mint_session(self, user_id: str, ttl_sec: int | None = None) -> str:
        """Mint a short-lived token bound to an already-authenticated user id.

        Used by the AI copilot so a `claude -p` / `codex exec` subprocess can call
        back into this server's /mcp AS the acting user (real ACL + source='ai'),
        without ever reusing the user's long-lived browser session in a CLI config
        file. Callers pass a short ttl (= the chat-turn timeout) and revoke with
        ``logout(token)`` when the turn ends.
        """
        uid = str(user_id or "").strip()
        if not uid:
            raise ZeError("validation_failed", "mint_session requires a user_id", context={})
        ttl = self._ttl_sec if ttl_sec is None else int(ttl_sec)
        if ttl <= 0:
            raise ZeError("validation_failed", f"ttl_sec must be positive, got {ttl!r}",
                          context={"ttl_sec": ttl})
        token = secrets.token_hex(SESSION_TOKEN_BYTES)
        now = time.time()
        info = _SessionInfo(user_id=uid, created_at=now, expires_at=now + ttl)
        with self._lock:
            if self._path is not None:
                with _exclusive_file_lock(self._path):
                    self._load()
                    self._sessions[token] = info
                    self._save()
            else:
                self._sessions[token] = info
        return token

    def logout(self, token: str) -> bool:
        with self._lock:
            if self._path is not None:
                with _exclusive_file_lock(self._path):
                    self._load()
                    existed = self._sessions.pop(token, None) is not None
                    if existed:
                        self._save()
                    return existed
            return self._sessions.pop(token, None) is not None

    def get_user_id(self, token: str) -> str | None:
        with self._lock:
            if self._path is not None:
                with _exclusive_file_lock(self._path):
                    self._load()
                    info = self._sessions.get(token)
                    if info is None:
                        return None
                    if time.time() >= info.expires_at:
                        self._sessions.pop(token, None)
                        self._save()
                        return None
                    return info.user_id
            info = self._sessions.get(token)
            if info is None:
                return None
            if time.time() >= info.expires_at:
                self._sessions.pop(token, None)
                return None
            return info.user_id

    def is_valid(self, token: str) -> bool:
        return self.get_user_id(token) is not None

    def cleanup_expired(self) -> int:
        with self._lock:
            if self._path is not None:
                with _exclusive_file_lock(self._path):
                    self._load()
                    now = time.time()
                    expired = [t for t, info in self._sessions.items() if now >= info.expires_at]
                    for t in expired:
                        self._sessions.pop(t, None)
                    if expired:
                        self._save()
                    return len(expired)
            now = time.time()
            expired = [t for t, info in self._sessions.items() if now >= info.expires_at]
            for t in expired:
                self._sessions.pop(t, None)
            return len(expired)

    def active_count(self) -> int:
        """当前 session 总数 (含过期未清的; 准实时统计用 cleanup_expired 后再调)."""
        with self._lock:
            if self._path is not None:
                with _exclusive_file_lock(self._path):
                    self._load()
            return len(self._sessions)


# ==============================================================
# PolicyStore / AuditLog — ZE-system 权限与审计
# ==============================================================

class PolicyStore:
    """按工具权限策略表。

    文件形态:
      {
        "schema_version": "1.0.0",
        "default_permission": "read",
        "users": {
          "feishu:ou_xxx": {
            "tool_permissions": {"ZE-tool-id": "edit"},
            "tool_model_scopes": {"ZE-tool-id": {"llm": true, "image": false}}
          }
        }
      }

    Hub 写、工具读;每次读都重新载入文件,保证策略修改无需重启业务工具。
    """

    def __init__(self, path: str | Path | None = None,
                 default_permission: str = PERMISSION_READ) -> None:
        self._path = Path(path).expanduser().resolve() if path else default_policies_path()
        self._default_permission = _clean_permission(default_permission)
        self._permissions: dict[str, dict[str, str]] = {}
        self._model_scopes: dict[str, dict[str, dict[str, bool]]] = {}
        self._tool_defaults: dict[str, str] = {}
        self._access_requests: dict[str, dict[str, Any]] = {}
        with _exclusive_file_lock(self._path):
            self._load()
            if not self._path.exists():
                self._save()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def default_permission(self) -> str:
        with _exclusive_file_lock(self._path):
            self._load()
            return self._default_permission

    def _load(self) -> None:
        if not self._path.exists():
            self._permissions = {}
            self._model_scopes = {}
            self._tool_defaults = {}
            self._access_requests = {}
            return
        raw = _read_json_file(self._path)
        if not isinstance(raw, dict):
            self._permissions = {}
            self._model_scopes = {}
            self._tool_defaults = {}
            self._access_requests = {}
            return
        self._default_permission = _clean_permission(
            raw.get("default_permission"),
            default=self._default_permission or PERMISSION_READ,
        )
        raw_users = raw.get("users") or {}
        permissions: dict[str, dict[str, str]] = {}
        model_scopes: dict[str, dict[str, dict[str, bool]]] = {}
        if isinstance(raw_users, dict):
            for user_id, row in raw_users.items():
                if not isinstance(user_id, str) or not isinstance(row, dict):
                    continue
                perms = row.get("tool_permissions") or {}
                clean: dict[str, str] = {}
                if isinstance(perms, dict):
                    for tool_id, permission in perms.items():
                        if isinstance(tool_id, str) and tool_id:
                            clean[tool_id] = _clean_permission(permission)
                permissions[user_id] = clean
                scopes_raw = row.get("tool_model_scopes") or {}
                scopes_by_tool: dict[str, dict[str, bool]] = {}
                if isinstance(scopes_raw, dict):
                    for tool_id, scopes in scopes_raw.items():
                        if not isinstance(tool_id, str) or not tool_id or not isinstance(scopes, dict):
                            continue
                        clean_scopes: dict[str, bool] = {}
                        for scope, enabled in scopes.items():
                            try:
                                clean_scopes[_clean_model_scope(scope)] = bool(enabled)
                            except ZeError:
                                continue
                        if clean_scopes:
                            scopes_by_tool[tool_id] = clean_scopes
                model_scopes[user_id] = scopes_by_tool
        self._permissions = permissions
        self._model_scopes = model_scopes

        tool_defaults: dict[str, str] = {}
        raw_defaults = raw.get("tool_defaults") or {}
        if isinstance(raw_defaults, dict):
            for tool_id, permission in raw_defaults.items():
                if isinstance(tool_id, str) and tool_id:
                    tool_defaults[tool_id] = _clean_permission(
                        permission, default=self._default_permission)
        self._tool_defaults = tool_defaults

        requests: dict[str, dict[str, Any]] = {}
        raw_requests = raw.get("access_requests") or {}
        if isinstance(raw_requests, dict):
            for key, row in raw_requests.items():
                if not isinstance(key, str) or not isinstance(row, dict):
                    continue
                user_id = str(row.get("user_id") or "")
                tool_id = str(row.get("tool_id") or "")
                if not user_id or not tool_id:
                    continue
                requests[_request_key(user_id, tool_id)] = {
                    "user_id": user_id,
                    "tool_id": tool_id,
                    "requested": _clean_permission(row.get("requested"), default=PERMISSION_READ),
                    "state": _clean_request_state(row.get("state")),
                    "reason": str(row.get("reason") or ""),
                    "created_at": float(row.get("created_at") or 0.0),
                    "decided_at": float(row.get("decided_at") or 0.0),
                    "decided_by": str(row.get("decided_by") or ""),
                }
        self._access_requests = requests

    def _save(self) -> None:
        users: dict[str, dict[str, Any]] = {}
        all_user_ids = set(self._permissions) | set(self._model_scopes)
        for user_id in sorted(all_user_ids):
            perms = self._permissions.get(user_id) or {}
            scopes = self._model_scopes.get(user_id) or {}
            row: dict[str, Any] = {"tool_permissions": dict(sorted(perms.items()))}
            if scopes:
                row["tool_model_scopes"] = {
                    tool_id: dict(sorted(scope_map.items()))
                    for tool_id, scope_map in sorted(scopes.items())
                    if scope_map
                }
            users[user_id] = row
        payload = {
            "schema_version": POLICY_STORE_SCHEMA_VERSION,
            "default_permission": self._default_permission,
            "tool_defaults": dict(sorted(self._tool_defaults.items())),
            "users": users,
            "access_requests": {
                key: dict(sorted(row.items()))
                for key, row in sorted(self._access_requests.items())
            },
        }
        _write_json_file(self._path, payload, mode=0o600)

    def get_permission(self, user_id: str, tool_id: str) -> str:
        if is_super_admin(user_id):
            return PERMISSION_EDIT
        with _exclusive_file_lock(self._path):
            self._load()
            perms = self._permissions.get(user_id) or {}
            explicit = perms.get(tool_id)
            if explicit is not None:
                return _clean_permission(explicit, default=self._default_permission)
            # 未显式授权 → 落该工具的默认档(标准 skill 设 none = 需申请),再落全局默认。
            return _clean_permission(
                self._tool_defaults.get(tool_id), default=self._default_permission)

    def set_permission(self, user_id: str, tool_id: str, permission: str) -> str:
        if not user_id:
            raise ZeError("validation_failed", "user_id is required")
        if not tool_id:
            raise ZeError("validation_failed", "tool_id is required")
        clean = _clean_permission(permission)
        with _exclusive_file_lock(self._path):
            self._load()
            self._permissions.setdefault(user_id, {})[tool_id] = clean
            self._save()
        return clean

    def clear_permission(self, user_id: str, tool_id: str) -> bool:
        """撤回显式授权,让这个人回到跟随工具默认档。

        没有这个操作的话,授权就是一扇单向门:一旦给过,再也回不到「跟随默认」——
        管理员只能把他钉死在某一档,之后改默认档对他不再生效。
        """
        if not user_id:
            raise ZeError("validation_failed", "user_id is required")
        if not tool_id:
            raise ZeError("validation_failed", "tool_id is required")
        with _exclusive_file_lock(self._path):
            self._load()
            perms = self._permissions.get(user_id) or {}
            if tool_id not in perms:
                return False
            perms.pop(tool_id, None)
            if perms:
                self._permissions[user_id] = perms
            else:
                self._permissions.pop(user_id, None)
            self._save()
        return True

    def get_model_scope_permission(self, user_id: str, tool_id: str, scope: str) -> bool:
        if is_super_admin(user_id):
            return True
        clean_scope = _clean_model_scope(scope)
        with _exclusive_file_lock(self._path):
            self._load()
            perms = self._permissions.get(user_id) or {}
            tool_permission = _clean_permission(
                perms.get(tool_id, self._tool_defaults.get(tool_id)),
                default=self._default_permission,
            )
            if not permission_allows(tool_permission, PERMISSION_READ):
                return False
            scopes = (self._model_scopes.get(user_id) or {}).get(tool_id) or {}
            return bool(scopes.get(clean_scope, True))

    def set_model_scope(self, user_id: str, tool_id: str, scope: str, enabled: bool) -> bool:
        if not user_id:
            raise ZeError("validation_failed", "user_id is required")
        if not tool_id:
            raise ZeError("validation_failed", "tool_id is required")
        clean_scope = _clean_model_scope(scope)
        with _exclusive_file_lock(self._path):
            self._load()
            self._model_scopes.setdefault(user_id, {}).setdefault(tool_id, {})[clean_scope] = bool(enabled)
            self._save()
        return bool(enabled)

    def user_permissions(self, user_id: str) -> dict[str, str]:
        with _exclusive_file_lock(self._path):
            self._load()
            return dict(self._permissions.get(user_id) or {})

    def user_model_scopes(self, user_id: str) -> dict[str, dict[str, bool]]:
        with _exclusive_file_lock(self._path):
            self._load()
            return {
                tool_id: dict(scopes)
                for tool_id, scopes in (self._model_scopes.get(user_id) or {}).items()
            }

    # ---------- 工具默认档 ----------

    def tool_default(self, tool_id: str) -> str:
        """该工具在「用户未被显式授权」时的档位。未设过则落全局默认。"""
        with _exclusive_file_lock(self._path):
            self._load()
            return _clean_permission(
                self._tool_defaults.get(tool_id), default=self._default_permission)

    def set_tool_default(self, tool_id: str, permission: str) -> str:
        if not tool_id:
            raise ZeError("validation_failed", "tool_id is required")
        clean = _clean_permission(permission, default=PERMISSION_NONE)
        with _exclusive_file_lock(self._path):
            self._load()
            self._tool_defaults[tool_id] = clean
            self._save()
        return clean

    def tool_defaults(self) -> dict[str, str]:
        with _exclusive_file_lock(self._path):
            self._load()
            return dict(sorted(self._tool_defaults.items()))

    # ---------- 权限申请 ----------

    def request_access(
        self,
        user_id: str,
        tool_id: str,
        *,
        requested: str = PERMISSION_READ,
        reason: str = "",
    ) -> dict[str, Any]:
        """提交权限申请。同一 用户×工具 只保留一行(重复提交=更新)。

        已被 ban 的用户不能申请;已经够用的档位也不必申请 —— 两者都抛 validation_failed,
        免得管理台待办里堆满无意义的行。
        """
        if not user_id:
            raise ZeError("validation_failed", "user_id is required")
        if not tool_id:
            raise ZeError("validation_failed", "tool_id is required")
        want = _clean_permission(requested, default=PERMISSION_READ)
        if not permission_allows(want, PERMISSION_READ):
            raise ZeError("validation_failed", f"cannot request permission {want!r}")
        current = self.get_permission(user_id, tool_id)
        if current == PERMISSION_BAN:
            raise ZeError("validation_failed", "banned user cannot request access")
        if permission_allows(current, want):
            raise ZeError("validation_failed", f"user already has {current!r} on {tool_id!r}")
        now = time.time()
        row = {
            "user_id": user_id,
            "tool_id": tool_id,
            "requested": want,
            "state": ACCESS_REQUEST_PENDING,
            "reason": str(reason or "")[:500],
            "created_at": now,
            "decided_at": 0.0,
            "decided_by": "",
        }
        with _exclusive_file_lock(self._path):
            self._load()
            self._access_requests[_request_key(user_id, tool_id)] = row
            self._save()
        return dict(row)

    def list_access_requests(self, state: str = "") -> list[dict[str, Any]]:
        """按状态列申请,新提交的排前面(管理台待办视图)。"""
        want = str(state or "").strip().lower()
        with _exclusive_file_lock(self._path):
            self._load()
            rows = [dict(r) for r in self._access_requests.values()]
        if want:
            rows = [r for r in rows if r.get("state") == want]
        rows.sort(key=lambda r: (-(r.get("created_at") or 0.0), r.get("user_id", "")))
        return rows

    def resolve_access_request(
        self,
        user_id: str,
        tool_id: str,
        *,
        approve: bool,
        decided_by: str = "",
    ) -> dict[str, Any]:
        """批准/驳回。批准会顺带把权限真正写进 tool_permissions —— 审批就是授权本身,
        不留「批了但没生效」的中间态。"""
        key = _request_key(user_id, tool_id)
        with _exclusive_file_lock(self._path):
            self._load()
            row = self._access_requests.get(key)
            if not row:
                raise ZeError("not_found", f"no access request for {user_id!r} on {tool_id!r}")
            if row.get("state") != ACCESS_REQUEST_PENDING:
                raise ZeError("validation_failed", f"request already {row.get('state')!r}")
            row = dict(row)
            row["state"] = (
                ACCESS_REQUEST_APPROVED if approve else ACCESS_REQUEST_REJECTED)
            row["decided_at"] = time.time()
            row["decided_by"] = str(decided_by or "")
            if approve:
                self._permissions.setdefault(user_id, {})[tool_id] = _clean_permission(
                    row.get("requested"), default=PERMISSION_READ)
            self._access_requests[key] = row
            self._save()
        return dict(row)

    def all_policies(self) -> dict[str, Any]:
        with _exclusive_file_lock(self._path):
            self._load()
            users: dict[str, dict[str, Any]] = {}
            all_user_ids = set(self._permissions) | set(self._model_scopes)
            for user_id in sorted(all_user_ids):
                perms = self._permissions.get(user_id) or {}
                scopes = self._model_scopes.get(user_id) or {}
                row: dict[str, Any] = {"tool_permissions": dict(sorted(perms.items()))}
                if scopes:
                    row["tool_model_scopes"] = {
                        tool_id: dict(sorted(scope_map.items()))
                        for tool_id, scope_map in sorted(scopes.items())
                        if scope_map
                    }
                users[user_id] = row
            return {
                "schema_version": POLICY_STORE_SCHEMA_VERSION,
                "default_permission": self._default_permission,
                "tool_defaults": dict(sorted(self._tool_defaults.items())),
                "users": users,
                "access_requests": [
                    dict(row) for row in sorted(
                        self._access_requests.values(),
                        key=lambda r: (-(r.get("created_at") or 0.0), r.get("user_id", "")),
                    )
                ],
            }


class AuditLog:
    """ZE-system 身份审计 NDJSON。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser().resolve() if path else default_audit_path()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event_type: str, *, actor_user_id: str = "",
               target_user_id: str = "", tool_id: str = "",
               source: str = "server", **details: Any) -> dict[str, Any]:
        event = {
            "schema_version": AUTH_AUDIT_SCHEMA_VERSION,
            "ts": time.time(),
            "event_type": event_type,
            "actor_user_id": actor_user_id,
            "target_user_id": target_user_id,
            "tool_id": tool_id,
            "source": source,
            "details": details,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self._path):
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def read(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                out.append(value)
            if len(out) >= limit:
                break
        out.reverse()
        return out


# ==============================================================
# User → Actor 桥
# ==============================================================

def build_actor(
    user: User, *,
    device_id: str = "default",
    source: str = "user",
) -> Actor:
    """User → Actor; user_id 直接用 user.user_id (= username)."""
    if not isinstance(user, User):
        raise ZeError(
            "validation_failed",
            f"build_actor expects User, got {type(user).__name__}",
            context={"type": type(user).__name__},
        )
    return Actor(user_id=user.user_id, device_id=device_id, source=source)


__all__ = [
    "User",
    "UserStore",
    "SessionManager",
    "PolicyStore",
    "AuditLog",
    "hash_password",
    "verify_password",
    "external_user_id",
    "build_actor",
    "user_has_channel",
    "user_public_card",
    "user_matches_query",
    "search_users",
    "lookup_users",
    "default_system_auth_dir",
    "default_users_path",
    "default_sessions_path",
    "default_policies_path",
    "default_audit_path",
    "DEFAULT_STORE_PATH",
    "DEFAULT_SESSION_TTL_SEC",
    "PERMISSION_NONE",
    "PERMISSION_READ",
    "PERMISSION_EDIT",
    "PERMISSION_BAN",
    "PERMISSIONS",
    "permission_allows",
    "ACCESS_REQUEST_PENDING",
    "ACCESS_REQUEST_APPROVED",
    "ACCESS_REQUEST_REJECTED",
    "ACCESS_REQUEST_STATES",
    "MODEL_SCOPE_LLM",
    "MODEL_SCOPE_IMAGE",
    "MODEL_SCOPE_VIDEO",
    "MODEL_SCOPE_VOICE",
    "MODEL_SCOPE_TTS",
    "MODEL_SCOPE_STT",
    "MODEL_SCOPE_EMBEDDING",
    "MODEL_SCOPES",
    "USER_STATUS_ACTIVE",
    "USER_STATUS_DISABLED",
    "USER_STATUSES",
    "super_admin_users",
    "is_super_admin",
]
