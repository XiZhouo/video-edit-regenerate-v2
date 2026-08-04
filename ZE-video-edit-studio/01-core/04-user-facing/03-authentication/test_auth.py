"""test_auth.py — User / UserStore / SessionManager / CommandRegistry 集成测试.

覆盖:
  - hash_password / verify_password: 随机 salt / round-trip / timing safe
  - User: to_dict / from_dict / verify_password
  - UserStore: register / authenticate / get / exists / list_users / 持久化
  - UserStore 边界: 重复注册 / 错密码 / 不存在 / 格式校验
  - UserStore 防枚举: 用户不存在跟密码错统一抛 auth_failed
  - SessionManager: login / logout / get_user_id / is_valid / TTL 过期 / cleanup
  - build_actor: User → Actor
  - CommandRegistry(enforce_login=True): 空 user_id 抛 auth_required
  - CommandRegistry(enforce_login=False): 默认行为不变 (兼容现有 526 测试)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# v3 平铺: 注入兄弟目录
_HERE = Path(__file__).resolve().parent
_CORE = _HERE.parent.parent
_PATHS = [
    _HERE,
    _CORE / "01-data-structure" / "01-document",
    _CORE / "01-data-structure" / "02-entity",
    _CORE / "03-commands-operations" / "01-command",
    _CORE / "03-commands-operations" / "02-operation",
]
for _p in _PATHS:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from auth import (
    AUTH_METHOD_FEISHU,
    AUTH_METHOD_LOCAL,
    AUTH_MODE_FEDERATED,
    AUTH_MODE_HYBRID,
    AUTH_MODE_MINIMAL_LOCAL,
    DEFAULT_SESSION_TTL_SEC,
    AuditLog,
    MODEL_SCOPE_IMAGE,
    MODEL_SCOPE_LLM,
    MODEL_SCOPE_VIDEO,
    PERMISSION_BAN,
    PERMISSION_EDIT,
    PERMISSION_READ,
    PolicyStore,
    SessionManager,
    User,
    UserStore,
    USER_STATUS_DISABLED,
    auth_runtime_config,
    build_actor,
    external_user_id,
    hash_password,
    lookup_users,
    normalize_local_login_name,
    search_users,
    user_has_channel,
    user_public_card,
    verify_password,
)
from command import CommandRegistry, CommandSpec
from document import Document
from entity import Actor, Entity, ZeError
from identity import resolve_actor
from operation import OpLog, Patch, new_operation


# ==============================================================
# 辅助
# ==============================================================

@pytest.fixture
def tmp_store(tmp_path):
    return UserStore(path=tmp_path / "users.json")


# ==============================================================
# 部署级登录入口
# ==============================================================

def test_auth_runtime_config_default_preserves_feishu_first_hybrid():
    config = auth_runtime_config({})
    assert config.mode == AUTH_MODE_HYBRID
    assert config.methods == (AUTH_METHOD_LOCAL, AUTH_METHOD_FEISHU)
    assert config.preferred_method == AUTH_METHOD_FEISHU
    assert config.registration_open is True
    assert config.public_dict()["email_verification_required"] is False


@pytest.mark.parametrize(
    ("mode", "methods", "preferred", "registration_open", "login_name_format"),
    [
        (AUTH_MODE_MINIMAL_LOCAL, (AUTH_METHOD_LOCAL,), AUTH_METHOD_LOCAL, True, "email"),
        (AUTH_MODE_FEDERATED, (AUTH_METHOD_FEISHU,), AUTH_METHOD_FEISHU, False, "username"),
        (AUTH_MODE_HYBRID, (AUTH_METHOD_LOCAL, AUTH_METHOD_FEISHU), AUTH_METHOD_FEISHU, True, "username"),
    ],
)
def test_auth_runtime_config_modes(mode, methods, preferred, registration_open, login_name_format):
    config = auth_runtime_config({"ZE_AUTH_MODE": mode})
    assert config.methods == methods
    assert config.preferred_method == preferred
    assert config.registration_open is registration_open
    assert config.login_name_format == login_name_format


def test_auth_runtime_config_rejects_explicit_disabled_preference():
    with pytest.raises(ZeError) as caught:
        auth_runtime_config({
            "ZE_AUTH_MODE": AUTH_MODE_MINIMAL_LOCAL,
            "ZE_AUTH_PREFERRED_METHOD": AUTH_METHOD_FEISHU,
        })
    assert caught.value.code == "auth_configuration_invalid"


def test_auth_runtime_config_federated_forces_registration_closed():
    config = auth_runtime_config({
        "ZE_AUTH_MODE": AUTH_MODE_FEDERATED,
        "ZE_AUTH_REGISTRATION": "open",
    })
    assert config.registration_open is False
    assert config.public_dict()["registration"] == "closed"


def test_email_login_name_is_normalized_but_not_claimed_verified():
    assert normalize_local_login_name(
        "  Alice+Art@Example.COM  ", login_name_format="email",
    ) == "alice+art@example.com"


@pytest.mark.parametrize("value", ("alice", "@example.com", "alice@example", "alice@-bad.com"))
def test_email_login_name_rejects_non_email_shapes(value):
    with pytest.raises(ZeError) as caught:
        normalize_local_login_name(value, login_name_format="email")
    assert caught.value.code == "validation_failed"


def test_username_login_name_preserves_legacy_identity_exactly():
    assert normalize_local_login_name("Alice", login_name_format="username") == "Alice"


# ==============================================================
# hash_password / verify_password
# ==============================================================

def test_hash_password_returns_64_hex_hash_and_32_hex_salt():
    h, s = hash_password("hunter222")
    assert len(h) == 64
    assert len(s) == 32
    int(h, 16)  # all hex
    int(s, 16)


def test_hash_password_random_salt_each_call():
    h1, s1 = hash_password("hunter222")
    h2, s2 = hash_password("hunter222")
    assert s1 != s2
    assert h1 != h2   # 相同密码不同 salt → 不同 hash


def test_hash_password_same_salt_same_hash():
    h1, s = hash_password("hunter222")
    h2, _ = hash_password("hunter222", salt=s)
    assert h1 == h2


def test_verify_password_round_trip():
    h, s = hash_password("hunter222")
    assert verify_password("hunter222", h, s) is True
    assert verify_password("hunter3", h, s) is False
    assert verify_password("", h, s) is False


def test_verify_password_non_str_returns_false():
    h, s = hash_password("hunter222")
    assert verify_password(None, h, s) is False  # type: ignore[arg-type]
    assert verify_password(42, h, s) is False    # type: ignore[arg-type]


def test_hash_password_non_str_raises():
    with pytest.raises(ZeError) as ex:
        hash_password(None)  # type: ignore[arg-type]
    assert ex.value.code == "validation_failed"


# ==============================================================
# User
# ==============================================================

def test_user_to_dict_from_dict_round_trip():
    h, s = hash_password("hunter222")
    u = User(user_id="alice", password_hash=h, password_salt=s)
    d = u.to_dict()
    u2 = User.from_dict(d)
    assert u2.user_id == "alice"
    assert u2.password_hash == h
    assert u2.password_salt == s
    assert u2.roles == ("user",)


def test_user_to_dict_roles_is_list():
    """list 才能 JSON 序列化."""
    h, s = hash_password("p")
    u = User(user_id="a", password_hash=h, password_salt=s, roles=("user", "admin"))
    d = u.to_dict()
    assert d["roles"] == ["user", "admin"]


def test_user_verify_password():
    h, s = hash_password("hunter222")   # 缺省 pbkdf2 → 记录需带 algo 标记(register/from_dict 会自动带)
    u = User(user_id="alice", password_hash=h, password_salt=s, password_algo="pbkdf2-sha256")
    assert u.verify_password("hunter222") is True
    assert u.verify_password("hunter3") is False


# ==============================================================
# UserStore: register
# ==============================================================

def test_register_creates_user(tmp_store):
    user = tmp_store.register("alice", "hunter222")
    assert user.user_id == "alice"
    assert user.verify_password("hunter222") is True
    assert tmp_store.exists("alice")


def test_register_persists_to_file(tmp_path):
    path = tmp_path / "users.json"
    store = UserStore(path=path)
    store.register("alice", "hunter222")
    # 重新构造一个 store, 应能读到 alice
    store2 = UserStore(path=path)
    u = store2.get("alice")
    assert u is not None
    assert u.user_id == "alice"
    assert u.verify_password("hunter222") is True


def test_register_duplicate_raises_user_exists(tmp_store):
    tmp_store.register("alice", "hunter222")
    with pytest.raises(ZeError) as ex:
        tmp_store.register("alice", "different")
    assert ex.value.code == "user_exists"


def test_register_empty_username_raises(tmp_store):
    with pytest.raises(ZeError) as ex:
        tmp_store.register("", "hunter222")
    assert ex.value.code == "validation_failed"


def test_register_whitespace_username_raises(tmp_store):
    with pytest.raises(ZeError) as ex:
        tmp_store.register("   ", "hunter222")
    assert ex.value.code == "validation_failed"


def test_register_too_long_username_raises(tmp_store):
    with pytest.raises(ZeError) as ex:
        tmp_store.register("a" * 200, "hunter222")
    assert ex.value.code == "validation_failed"


def test_register_username_with_illegal_chars(tmp_store):
    with pytest.raises(ZeError) as ex:
        tmp_store.register("alice:admin", "hunter222")
    assert ex.value.code == "validation_failed"


def test_register_reserved_dunder_prefix_rejected(tmp_store):
    """"__" 是框架保留伪账号前缀(__system__/__mcp__/__demo_*)。禁真实用户注册,否则可冒充
    这些身份套用凭据/scope 豁免与 document.set_acl owner-only 豁免(安全:身份边界)。"""
    for name in ("__system__", "__mcp__", "__demo_guest__", "__whatever"):
        with pytest.raises(ZeError) as ex:
            tmp_store.register(name, "hunter222")
        assert ex.value.code == "validation_failed"
    assert not tmp_store.exists("__system__")


def test_register_short_password_raises(tmp_store):
    with pytest.raises(ZeError) as ex:
        tmp_store.register("alice", "abc")
    assert ex.value.code == "validation_failed"


def test_register_empty_password_raises(tmp_store):
    with pytest.raises(ZeError) as ex:
        tmp_store.register("alice", "")
    assert ex.value.code == "validation_failed"


def test_register_whitespace_password_raises(tmp_store):
    with pytest.raises(ZeError) as ex:
        tmp_store.register("alice", "       ")
    assert ex.value.code == "validation_failed"


def test_register_password_with_leading_trailing_whitespace_raises(tmp_store):
    """前后空白被前端 trim 后会导致登录失败 — 注册时直接拒绝."""
    with pytest.raises(ZeError) as ex:
        tmp_store.register("alice", " hunter222 ")
    assert ex.value.code == "validation_failed"
    with pytest.raises(ZeError):
        tmp_store.register("alice", "hunter222 ")
    with pytest.raises(ZeError):
        tmp_store.register("alice", " hunter222")


def test_register_username_with_null_byte(tmp_store):
    with pytest.raises(ZeError) as ex:
        tmp_store.register("alice\x00admin", "hunter222")
    assert ex.value.code == "validation_failed"


def test_register_username_with_zero_width_space(tmp_store):
    """U+200B (ZWSP) 不应被允许 — 视觉上跟正常 username 一样, 易被钓鱼."""
    with pytest.raises(ZeError) as ex:
        tmp_store.register("alice​bob", "hunter222")
    assert ex.value.code == "validation_failed"


# ==============================================================
# UserStore: authenticate
# ==============================================================

def test_authenticate_correct_password(tmp_store):
    tmp_store.register("alice", "hunter222")
    user = tmp_store.authenticate("alice", "hunter222")
    assert user.user_id == "alice"


def test_record_login_preserves_password_algorithm(tmp_store):
    user = tmp_store.register("alice", "hunter222")
    assert user.password_algo == "pbkdf2-sha256"
    recorded = tmp_store.record_login(user.user_id, tool_id="test-tool")
    assert recorded is not None
    assert recorded.password_algo == "pbkdf2-sha256"
    assert tmp_store.authenticate("alice", "hunter222").user_id == "alice"


def test_authenticate_wrong_password_raises_auth_failed(tmp_store):
    tmp_store.register("alice", "hunter222")
    with pytest.raises(ZeError) as ex:
        tmp_store.authenticate("alice", "wrong")
    assert ex.value.code == "auth_failed"


def test_authenticate_nonexistent_user_raises_auth_failed(tmp_store):
    """防枚举: 跟密码错统一错误码."""
    with pytest.raises(ZeError) as ex:
        tmp_store.authenticate("nobody", "anything")
    assert ex.value.code == "auth_failed"


def test_authenticate_anti_enumeration_same_message(tmp_store):
    """两种失败原因应抛同一 code + 同一 message."""
    tmp_store.register("alice", "hunter222")
    try:
        tmp_store.authenticate("alice", "wrong")
    except ZeError as e1:
        msg1 = str(e1)
    try:
        tmp_store.authenticate("bob", "wrong")
    except ZeError as e2:
        msg2 = str(e2)
    assert msg1 == msg2


def test_authenticate_disabled_user_rejected_even_with_correct_password(tmp_store):
    """禁用用户即使密码正确也不能登(此前遗漏: authenticate 放行, 禁用只在后续访问工具时才拦
    → 被禁用户仍能签发有效 session token)。"""
    user = tmp_store.register("alice", "hunter222")
    tmp_store.set_status(user.user_id, USER_STATUS_DISABLED)
    with pytest.raises(ZeError) as ex:
        tmp_store.authenticate("alice", "hunter222")
    assert ex.value.code == "auth_failed"


def test_authenticate_non_str_inputs():
    store = UserStore(path=Path("/tmp/ze_test_auth_x.json"))
    try:
        with pytest.raises(ZeError) as ex:
            store.authenticate(None, "anything")  # type: ignore[arg-type]
        assert ex.value.code == "auth_failed"
    finally:
        if Path("/tmp/ze_test_auth_x.json").exists():
            Path("/tmp/ze_test_auth_x.json").unlink()


# ==============================================================
# UserStore: get / exists / list_users
# ==============================================================

def test_get_existing(tmp_store):
    tmp_store.register("alice", "hunter222")
    u = tmp_store.get("alice")
    assert u is not None and u.user_id == "alice"


def test_get_missing_returns_none(tmp_store):
    assert tmp_store.get("nobody") is None


def test_exists(tmp_store):
    tmp_store.register("alice", "hunter222")
    assert tmp_store.exists("alice")
    assert not tmp_store.exists("bob")


def test_list_users(tmp_store):
    tmp_store.register("alice", "hunter222")
    tmp_store.register("bob", "topsecret")
    users = tmp_store.list_users()
    assert {u.user_id for u in users} == {"alice", "bob"}


# ==============================================================
# UserStore: external OAuth users
# ==============================================================

def test_external_user_id_prefixes_provider():
    assert external_user_id("feishu", "ou_123") == "feishu:ou_123"


def test_upsert_external_user_creates_feishu_user(tmp_store):
    user = tmp_store.upsert_external_user(
        provider="feishu",
        external_id="ou_123",
        display_name="张三",
        email="zhangsan@example.com",
        avatar_url="https://example.com/a.png",
    )
    assert user.user_id == "feishu:ou_123"
    assert user.auth_provider == "feishu"
    assert user.external_id == "ou_123"
    assert user.display_name == "张三"
    assert user.email == "zhangsan@example.com"
    assert user.verify_password("anything") is False
    assert tmp_store.get("feishu:ou_123") == user


def test_upsert_external_user_updates_existing_profile(tmp_store):
    first = tmp_store.upsert_external_user(
        provider="feishu",
        external_id="ou_123",
        display_name="Old",
        email="old@example.com",
    )
    second = tmp_store.upsert_external_user(
        provider="feishu",
        external_id="ou_123",
        display_name="New",
        email="new@example.com",
        avatar_url="https://example.com/new.png",
    )
    assert second.user_id == first.user_id
    assert second.created_at == first.created_at
    assert second.display_name == "New"
    assert second.email == "new@example.com"
    assert second.avatar_url == "https://example.com/new.png"
    assert len(tmp_store.list_users()) == 1


def test_upsert_external_user_persists_to_file(tmp_path):
    path = tmp_path / "users.json"
    store = UserStore(path=path)
    store.upsert_external_user(
        provider="feishu",
        external_id="ou_123",
        display_name="张三",
    )
    loaded = UserStore(path=path).get("feishu:ou_123")
    assert loaded is not None
    assert loaded.auth_provider == "feishu"
    assert loaded.external_id == "ou_123"
    assert loaded.display_name == "张三"


def test_upsert_external_user_records_login_metadata(tmp_store):
    user = tmp_store.upsert_external_user(
        provider="feishu",
        external_id="ou_123",
        display_name="张三",
        tool_id="ZE-demo-tool",
    )
    assert user.first_seen_at > 0
    assert user.last_seen_at >= user.first_seen_at
    assert user.last_login_at >= user.first_seen_at
    assert user.last_login_tool == "ZE-demo-tool"
    assert user.status == "active"


def test_set_status_disables_user(tmp_store):
    user = tmp_store.upsert_external_user(provider="feishu", external_id="ou_123")
    disabled = tmp_store.set_status(user.user_id, USER_STATUS_DISABLED)
    assert disabled.status == USER_STATUS_DISABLED
    assert tmp_store.get(user.user_id).status == USER_STATUS_DISABLED


def test_session_login_accepts_external_user(tmp_store):
    user = tmp_store.upsert_external_user(provider="feishu", external_id="ou_123")
    sm = SessionManager(ttl_sec=3600)
    token = sm.login(user)
    assert sm.get_user_id(token) == "feishu:ou_123"


def test_user_public_card_sanitizes_sensitive_fields(tmp_store):
    user = tmp_store.upsert_external_user(
        provider="feishu",
        external_id="ou_123",
        display_name="张三",
        email="z@example.com",
    )
    card = user_public_card(user, permission="edit", tool_id="ZE-demo")
    assert card["id"] == "feishu:ou_123"
    assert card["name"] == "张三"
    assert card["email"] == "z@example.com"
    assert card["permission"] == "edit"
    assert card["tool_id"] == "ZE-demo"
    assert card["channels"]["feishu"] is True
    assert "password_hash" not in card
    assert "password_salt" not in card
    assert "external_id" not in card


def test_upsert_external_disabled_user_rejected_on_relogin(tmp_store):
    """禁用的外部(飞书)用户重新登录(upsert)应被拒 —— 补 authenticate 之外的 external 登录路径
    (code-review CRITICAL: lark/sse 走 upsert+login 不经 authenticate, 禁用用户仍签发 token)。
    注意: 首次 upsert 创建时 existing 为 None、不触发(与目录测试的先建后禁模式不冲突)。"""
    user = tmp_store.upsert_external_user(provider="feishu", external_id="ou_x")
    tmp_store.set_status(user.user_id, USER_STATUS_DISABLED)
    with pytest.raises(ZeError) as ex:
        tmp_store.upsert_external_user(provider="feishu", external_id="ou_x")
    assert ex.value.code == "auth_failed"


def test_user_directory_search_filters_disabled_and_channel(tmp_store):
    feishu = tmp_store.upsert_external_user(
        provider="feishu",
        external_id="ou_123",
        display_name="赵舒雨",
        email="shuyu@example.com",
    )
    local = tmp_store.register("local_user", "hunter222")
    disabled = tmp_store.upsert_external_user(provider="feishu", external_id="ou_disabled")
    tmp_store.set_status(disabled.user_id, USER_STATUS_DISABLED)

    users = tmp_store.list_users()
    assert user_has_channel(feishu, "feishu") is True
    assert user_has_channel(local, "feishu") is False
    assert [u.user_id for u in search_users(users, query="shuyu")] == [feishu.user_id]
    assert [u.user_id for u in search_users(users, channel="feishu")] == [feishu.user_id]


def test_user_directory_lookup_preserves_order_and_reports_missing(tmp_store):
    a = tmp_store.upsert_external_user(provider="feishu", external_id="ou_a")
    b = tmp_store.register("bob", "hunter222")
    disabled = tmp_store.upsert_external_user(provider="feishu", external_id="ou_disabled")
    tmp_store.set_status(disabled.user_id, USER_STATUS_DISABLED)

    found, missing = lookup_users(
        tmp_store.list_users(),
        [b.user_id, "none", a.user_id, disabled.user_id],
    )
    assert [u.user_id for u in found] == [b.user_id, a.user_id]
    assert missing == ["none", disabled.user_id]


# ==============================================================
# PolicyStore / AuditLog
# ==============================================================

def test_policy_store_defaults_to_read(tmp_path):
    policy = PolicyStore(path=tmp_path / "policies.json")
    assert policy.get_permission("feishu:ou_123", "ZE-tool-a") == PERMISSION_READ


def test_policy_store_sets_per_tool_permission(tmp_path):
    policy = PolicyStore(path=tmp_path / "policies.json")
    assert policy.set_permission("feishu:ou_123", "ZE-tool-a", "edit") == PERMISSION_EDIT
    assert policy.set_permission("feishu:ou_123", "ZE-tool-b", "ban") == PERMISSION_BAN

    loaded = PolicyStore(path=tmp_path / "policies.json")
    assert loaded.get_permission("feishu:ou_123", "ZE-tool-a") == PERMISSION_EDIT
    assert loaded.get_permission("feishu:ou_123", "ZE-tool-b") == PERMISSION_BAN
    assert loaded.get_permission("feishu:ou_123", "ZE-tool-c") == PERMISSION_READ


def test_policy_store_super_admin_is_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SUPER_ADMIN_USERS", "feishu:ou_admin")
    policy = PolicyStore(path=tmp_path / "policies.json")
    policy.set_permission("feishu:ou_admin", "ZE-tool-a", "ban")
    assert policy.get_permission("feishu:ou_admin", "ZE-tool-a") == PERMISSION_EDIT


def test_policy_store_model_scopes_default_allow_and_can_deny(tmp_path):
    policy = PolicyStore(path=tmp_path / "policies.json")
    assert policy.get_model_scope_permission("feishu:ou_123", "ZE-tool-a", MODEL_SCOPE_LLM) is True

    assert policy.set_model_scope("feishu:ou_123", "ZE-tool-a", MODEL_SCOPE_LLM, False) is False
    assert policy.get_model_scope_permission("feishu:ou_123", "ZE-tool-a", MODEL_SCOPE_LLM) is False
    assert policy.get_model_scope_permission("feishu:ou_123", "ZE-tool-a", MODEL_SCOPE_IMAGE) is True

    loaded = PolicyStore(path=tmp_path / "policies.json")
    assert loaded.user_model_scopes("feishu:ou_123") == {"ZE-tool-a": {MODEL_SCOPE_LLM: False}}
    assert loaded.get_model_scope_permission("feishu:ou_123", "ZE-tool-a", MODEL_SCOPE_LLM) is False


def test_policy_store_ban_overrides_model_scope_allow(tmp_path):
    policy = PolicyStore(path=tmp_path / "policies.json")
    policy.set_model_scope("feishu:ou_123", "ZE-tool-a", MODEL_SCOPE_VIDEO, True)
    policy.set_permission("feishu:ou_123", "ZE-tool-a", "ban")
    assert policy.get_model_scope_permission("feishu:ou_123", "ZE-tool-a", MODEL_SCOPE_VIDEO) is False


def test_policy_store_super_admin_overrides_model_scope_deny(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_SUPER_ADMIN_USERS", "feishu:ou_admin")
    policy = PolicyStore(path=tmp_path / "policies.json")
    policy.set_model_scope("feishu:ou_admin", "ZE-tool-a", MODEL_SCOPE_LLM, False)
    policy.set_permission("feishu:ou_admin", "ZE-tool-a", "ban")
    assert policy.get_model_scope_permission("feishu:ou_admin", "ZE-tool-a", MODEL_SCOPE_LLM) is True


def test_audit_log_appends_and_reads(tmp_path):
    audit = AuditLog(path=tmp_path / "auth_audit.ndjson")
    audit.append(
        "tool_permission_changed",
        actor_user_id="feishu:ou_admin",
        target_user_id="feishu:ou_123",
        tool_id="ZE-tool-a",
        permission="edit",
    )
    rows = audit.read()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "tool_permission_changed"
    assert rows[0]["target_user_id"] == "feishu:ou_123"
    assert rows[0]["details"]["permission"] == "edit"


# ==============================================================
# UserStore: 文件健壮性
# ==============================================================

def test_corrupted_file_falls_back_to_empty(tmp_path):
    path = tmp_path / "users.json"
    path.write_text("{ not json", encoding="utf-8")
    store = UserStore(path=path)
    # 不抛; 空 store
    assert store.list_users() == []
    # 仍能正常注册
    store.register("alice", "hunter222")
    assert store.exists("alice")


def test_missing_file_init_empty(tmp_path):
    store = UserStore(path=tmp_path / "nonexistent.json")
    assert store.list_users() == []


def test_persistence_format_has_schema_version(tmp_store):
    tmp_store.register("alice", "hunter222")
    with open(tmp_store.path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    assert raw.get("schema_version") == "1.0.0"
    assert isinstance(raw.get("users"), list)


def test_password_hash_not_plaintext(tmp_store):
    tmp_store.register("alice", "hunter222")
    text = tmp_store.path.read_text()
    assert "hunter222" not in text  # 明文不进文件


# ==============================================================
# SessionManager
# ==============================================================

@pytest.fixture
def session_mgr():
    return SessionManager(ttl_sec=3600)


def test_session_default_ttl():
    sm = SessionManager()
    assert sm.active_count() == 0


def test_session_invalid_ttl_raises():
    with pytest.raises(ZeError):
        SessionManager(ttl_sec=0)
    with pytest.raises(ZeError):
        SessionManager(ttl_sec=-1)


def test_login_returns_token(session_mgr, tmp_store):
    user = tmp_store.register("alice", "hunter222")
    token = session_mgr.login(user)
    assert isinstance(token, str)
    assert len(token) == 64    # 32 bytes = 64 hex


def test_login_tokens_unique(session_mgr, tmp_store):
    user = tmp_store.register("alice", "hunter222")
    t1 = session_mgr.login(user)
    t2 = session_mgr.login(user)
    assert t1 != t2


def test_get_user_id_after_login(session_mgr, tmp_store):
    user = tmp_store.register("alice", "hunter222")
    token = session_mgr.login(user)
    assert session_mgr.get_user_id(token) == "alice"


def test_mint_session_binds_user_and_is_revocable(session_mgr):
    # ephemeral token for the AI copilot: no User object, just an authenticated id
    token = session_mgr.mint_session("feishu:ou_x", ttl_sec=60)
    assert isinstance(token, str) and len(token) == 64
    assert session_mgr.get_user_id(token) == "feishu:ou_x"
    assert session_mgr.logout(token) is True          # revoke on turn end
    assert session_mgr.get_user_id(token) is None


def test_mint_session_expires(session_mgr, monkeypatch):
    import auth as _auth
    base = 1_000_000.0
    monkeypatch.setattr(_auth.time, "time", lambda: base)
    token = session_mgr.mint_session("feishu:ou_x", ttl_sec=10)
    monkeypatch.setattr(_auth.time, "time", lambda: base + 11)
    assert session_mgr.get_user_id(token) is None


def test_mint_session_validates_inputs(session_mgr):
    with pytest.raises(ZeError):
        session_mgr.mint_session("", ttl_sec=60)
    with pytest.raises(ZeError):
        session_mgr.mint_session("feishu:ou_x", ttl_sec=0)


def test_get_user_id_unknown_token_returns_none(session_mgr):
    assert session_mgr.get_user_id("nope") is None


def test_logout_invalidates_token(session_mgr, tmp_store):
    user = tmp_store.register("alice", "hunter222")
    token = session_mgr.login(user)
    assert session_mgr.logout(token) is True
    assert session_mgr.get_user_id(token) is None
    # 再 logout 返 False
    assert session_mgr.logout(token) is False


def test_is_valid(session_mgr, tmp_store):
    user = tmp_store.register("alice", "hunter222")
    token = session_mgr.login(user)
    assert session_mgr.is_valid(token) is True
    session_mgr.logout(token)
    assert session_mgr.is_valid(token) is False


@pytest.mark.slow   # C3:墙钟型(sleep/TTL),归 pytest-slow 门
def test_session_expired_returns_none(tmp_store):
    sm = SessionManager(ttl_sec=1)
    user = tmp_store.register("alice", "hunter222")
    token = sm.login(user)
    assert sm.get_user_id(token) == "alice"
    time.sleep(1.1)
    assert sm.get_user_id(token) is None
    # 顺手清; active_count 应 0
    assert sm.active_count() == 0


@pytest.mark.slow   # C3:墙钟型(sleep/TTL),归 pytest-slow 门
def test_cleanup_expired_batch(tmp_store):
    sm = SessionManager(ttl_sec=1)
    user = tmp_store.register("alice", "hunter222")
    for _ in range(3):
        sm.login(user)
    assert sm.active_count() == 3
    time.sleep(1.1)
    cleared = sm.cleanup_expired()
    assert cleared == 3
    assert sm.active_count() == 0


def test_session_persists_when_path_is_set(tmp_path, tmp_store):
    path = tmp_path / "sessions.json"
    user = tmp_store.register("alice", "hunter222")
    sm = SessionManager(ttl_sec=3600, path=path)
    token = sm.login(user)

    sm2 = SessionManager(ttl_sec=3600, path=path)
    assert sm2.get_user_id(token) == "alice"
    assert sm2.active_count() == 1


def test_session_load_drops_expired_entries(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({
        "schema_version": "1.0.0",
        "sessions": {
            "expired-token": {
                "user_id": "alice",
                "created_at": time.time() - 10,
                "expires_at": time.time() - 1,
            },
        },
    }), encoding="utf-8")

    sm = SessionManager(ttl_sec=3600, path=path)
    assert sm.get_user_id("expired-token") is None
    assert sm.active_count() == 0


def test_login_non_user_raises(session_mgr):
    with pytest.raises(ZeError):
        session_mgr.login("not a user")  # type: ignore[arg-type]


# ==============================================================
# build_actor
# ==============================================================

def test_build_actor(tmp_store):
    user = tmp_store.register("alice", "hunter222")
    actor = build_actor(user)
    assert isinstance(actor, Actor)
    assert actor.user_id == "alice"
    assert actor.source == "user"
    assert actor.device_id == "default"


def test_build_actor_custom_fields(tmp_store):
    user = tmp_store.register("alice", "hunter222")
    actor = build_actor(user, device_id="laptop", source="ai")
    assert actor.device_id == "laptop"
    assert actor.source == "ai"


def test_build_actor_non_user_raises():
    with pytest.raises(ZeError):
        build_actor("alice")  # type: ignore[arg-type]


# ==============================================================
# CommandRegistry enforce_login 集成
# ==============================================================

def _make_doc_with_entity():
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    return doc


def _make_bump_spec():
    def execute_fn(*, args, actor, source, doc):
        return new_operation(
            command_id="bump", source=source, actor=actor.to_dict(),
            patches=[Patch("t", "e1", "x", "set", 1, 2)],
            reverse_patches=[Patch("t", "e1", "x", "set", 2, 1)],
        )
    return CommandSpec(id="bump", label="Bump", execute=execute_fn)


def test_dispatch_without_enforce_login_allows_empty_user_id():
    """默认 enforce_login=False: 兼容现有测试."""
    reg = CommandRegistry()
    reg.register(_make_bump_spec())
    doc = _make_doc_with_entity()
    op_log = OpLog()
    # 空 user_id 应正常 dispatch
    actor = Actor(user_id="", source="user")
    op = reg.dispatch("bump", {}, actor=actor, source="user", doc=doc, op_log=op_log)
    assert op.command_id == "bump"


def test_dispatch_enforce_login_rejects_empty_user_id():
    reg = CommandRegistry(enforce_login=True)
    reg.register(_make_bump_spec())
    doc = _make_doc_with_entity()
    op_log = OpLog()
    actor = Actor(user_id="", source="user")
    with pytest.raises(ZeError) as ex:
        reg.dispatch("bump", {}, actor=actor, source="user", doc=doc, op_log=op_log)
    assert ex.value.code == "auth_required"
    # op_log 不应有这条 (拒绝在 dispatch 内)
    assert len(op_log) == 0


def test_dispatch_enforce_login_accepts_filled_user_id():
    reg = CommandRegistry(enforce_login=True)
    reg.register(_make_bump_spec())
    doc = _make_doc_with_entity()
    op_log = OpLog()
    actor = Actor(user_id="alice", source="user")
    op = reg.dispatch("bump", {}, actor=actor, source="user", doc=doc, op_log=op_log)
    assert op.command_id == "bump"
    assert op.actor["user_id"] == "alice"
    # W03: auth gate only executes the handler; persistence belongs to CommitCoordinator.
    assert len(op_log) == 0


def test_dispatch_enforce_login_no_source_bypass():
    """source=system 不再例外 — 信任 caller 不安全;
    framework 自动化任务应用一个保留账号 (e.g. user_id='__system__') 主动登录."""
    reg = CommandRegistry(enforce_login=True)
    reg.register(_make_bump_spec())
    doc = _make_doc_with_entity()
    op_log = OpLog()
    # 空 user_id + source=system → 仍然被拦
    actor = Actor(user_id="", source="system")
    with pytest.raises(ZeError) as ex:
        reg.dispatch("bump", {}, actor=actor, source="system", doc=doc, op_log=op_log)
    assert ex.value.code == "auth_required"
    # 给 system 账号配 user_id 后能跑
    actor = Actor(user_id="__system__", source="system")
    op = reg.dispatch("bump", {}, actor=actor, source="system", doc=doc, op_log=op_log)
    assert op.command_id == "bump"


# ==============================================================
# 端到端: 注册 → 登录 → token → actor → dispatch
# ==============================================================

def test_end_to_end_register_login_dispatch(tmp_store):
    """SPEC §6 标准接入模式."""
    # 1. 注册
    tmp_store.register("alice", "hunter222")
    # 2. 登录
    user = tmp_store.authenticate("alice", "hunter222")
    sm = SessionManager()
    token = sm.login(user)
    # 3. 模拟"前端带 token 调请求"; 后端从 token 还原
    user_id = sm.get_user_id(token)
    assert user_id == "alice"
    loaded = tmp_store.get(user_id)
    # 4. 用还原的 user 造 actor + dispatch
    actor = build_actor(loaded)
    reg = CommandRegistry(enforce_login=True)
    reg.register(_make_bump_spec())
    doc = _make_doc_with_entity()
    op_log = OpLog()
    op = reg.dispatch("bump", {}, actor=actor, source="user", doc=doc, op_log=op_log)
    # 5. handler 产生的 Operation 已保留 alice；真正 append 由提交协调器统一完成。
    assert op.actor["user_id"] == "alice"
    assert len(op_log) == 0


def test_end_to_end_unauthorized_blocked(tmp_store):
    """未登录的请求 (空 token) 应在 web layer 拒绝;
    框架兜底: enforce_login=True 时 dispatch 也拒."""
    sm = SessionManager()
    # 模拟前端伪造一个不存在的 token
    user_id = sm.get_user_id("forged_token_xyz")
    assert user_id is None
    # web layer 此时应该 401; 但若放过来到 dispatch, 框架仍拦
    reg = CommandRegistry(enforce_login=True)
    reg.register(_make_bump_spec())
    doc = _make_doc_with_entity()
    op_log = OpLog()
    actor = Actor(user_id="", source="user")
    with pytest.raises(ZeError) as ex:
        reg.dispatch("bump", {}, actor=actor, source="user", doc=doc, op_log=op_log)
    assert ex.value.code == "auth_required"


# ==============================================================
# 2026-07 安全三项①②:pbkdf2 缺省 + 惰性迁移 + 登录限流
# ==============================================================

import auth as _auth  # noqa: E402  模块属性(算法常量/限流参数)供 monkeypatch


def test_hash_password_default_is_pbkdf2():
    h_new, salt = _auth.hash_password("longpassword")
    h_legacy, _ = _auth.hash_password("longpassword", salt, algo=_auth.LEGACY_PASSWORD_ALGO)
    assert h_new != h_legacy and len(h_new) == 64
    assert _auth.verify_password("longpassword", h_new, salt, algo=_auth.PBKDF2_PASSWORD_ALGO)
    assert not _auth.verify_password("wrongpass1", h_new, salt, algo=_auth.PBKDF2_PASSWORD_ALGO)


def test_register_stamps_pbkdf2_algo(tmp_store):
    u = tmp_store.register("alice", "hunter222")
    assert u.password_algo == _auth.PBKDF2_PASSWORD_ALGO


def test_legacy_sha256_user_migrates_on_login(tmp_path, monkeypatch):
    """旧 sha256 存量:登录验证通过 → 当场重哈希 pbkdf2 落盘(不强制重设密码)。"""
    monkeypatch.setattr(_auth, "PBKDF2_ITERATIONS", 1000)   # 提速,不影响语义
    store = _auth.UserStore(tmp_path / "users.json")
    store.register("alice", "hunter222")
    u = store._users["alice"]                               # 人工降级成历史记录形态
    h, s = _auth.hash_password("hunter222", algo=_auth.LEGACY_PASSWORD_ALGO)
    u.password_hash, u.password_salt, u.password_algo = h, s, ""
    store._save()
    got = store.authenticate("alice", "hunter222")
    assert got.password_algo == _auth.PBKDF2_PASSWORD_ALGO   # 返回值已迁移
    fresh = _auth.UserStore(tmp_path / "users.json")         # 落盘持久
    assert fresh._users["alice"].password_algo == _auth.PBKDF2_PASSWORD_ALGO
    assert fresh._users["alice"].password_hash != h          # 真换了哈希
    assert fresh.authenticate("alice", "hunter222").user_id == "alice"


def test_min_password_len_is_8(tmp_store):
    with pytest.raises(ZeError) as e:
        tmp_store.register("bob", "1234567")
    assert e.value.code == "validation_failed"
    tmp_store.register("bob", "12345678")


def test_explicit_bearer_identity_overrides_ambient_cookie(tmp_path):
    users = UserStore(path=tmp_path / "users.json")
    alice = users.register("alice", "alice-pass-1")
    bob = users.register("bob", "bob-pass-1")
    sessions = SessionManager(ttl_sec=3600)
    alice_token = sessions.login(alice)
    bob_token = sessions.login(bob)
    actor = resolve_actor(
        authorization=f"Bearer {alice_token}",
        cookie_token=bob_token,
        query_token=bob_token,
        user_store=users,
        session_mgr=sessions,
    )
    assert actor.user_id == "alice"


def test_login_rate_limit_exponential_and_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(_auth, "PBKDF2_ITERATIONS", 1000)
    monkeypatch.setattr(_auth, "_RL_BASE_LOCK_SEC", 0.05)
    store = _auth.UserStore(tmp_path / "users.json")
    store.register("alice", "hunter222")
    for _ in range(5):                                       # 连续 5 次失败(第 5 次挂锁)
        with pytest.raises(ZeError) as e:
            store.authenticate("alice", "wrong-pass")
        assert e.value.code == "auth_failed"
    with pytest.raises(ZeError) as e:                        # 锁窗内即刻再试 → 限流
        store.authenticate("alice", "hunter222")
    assert e.value.code == "rate_limited"
    time.sleep(0.12)
    got = store.authenticate("alice", "hunter222")           # 锁过期,正确密码可登且清零
    assert got.user_id == "alice"
    with pytest.raises(ZeError) as e:                        # 清零后单次失败不触锁
        store.authenticate("alice", "wrong-pass")
    assert e.value.code == "auth_failed"
    store.authenticate("alice", "hunter222")                 # 仍可正常登录


def test_login_rate_limit_covers_unknown_username(tmp_path, monkeypatch):
    """不存在的用户同样记账(路径一致防枚举:限流行为不泄露存在性)。"""
    monkeypatch.setattr(_auth, "PBKDF2_ITERATIONS", 1000)
    monkeypatch.setattr(_auth, "_RL_BASE_LOCK_SEC", 0.05)
    store = _auth.UserStore(tmp_path / "users.json")
    for _ in range(5):
        with pytest.raises(ZeError) as e:
            store.authenticate("ghost-user", "whatever-pw")
        assert e.value.code == "auth_failed"
    with pytest.raises(ZeError) as e:
        store.authenticate("ghost-user", "whatever-pw")
    assert e.value.code == "rate_limited"


# ==============================================================
# 权限档位 none + 工具默认档 + 申请流(POLICY_STORE 1.1.0)
# ==============================================================

def test_permission_allows_rejects_both_none_and_ban():
    """统一放行判定:none(未授权)与 ban(封禁)都不放行,read 不足以当 edit 用。"""
    assert _auth.permission_allows(_auth.PERMISSION_READ) is True
    assert _auth.permission_allows(_auth.PERMISSION_EDIT) is True
    assert _auth.permission_allows(_auth.PERMISSION_EDIT, _auth.PERMISSION_EDIT) is True
    assert _auth.permission_allows(_auth.PERMISSION_NONE) is False
    assert _auth.permission_allows(_auth.PERMISSION_BAN) is False
    assert _auth.permission_allows(_auth.PERMISSION_READ, _auth.PERMISSION_EDIT) is False
    assert _auth.permission_allows("garbage") is False          # 不认识的值按无权限,不按默认放行


def test_tool_default_none_denies_until_granted(tmp_path):
    """标准 skill 的默认档设 none:任何人默认无权限,显式授权后才放行。"""
    policy = _auth.PolicyStore(tmp_path / "policies.json")
    assert policy.get_permission("feishu:ou_1", "ZE-skill-build") == _auth.PERMISSION_READ  # 全局默认
    policy.set_tool_default("ZE-skill-build", _auth.PERMISSION_NONE)
    assert policy.get_permission("feishu:ou_1", "ZE-skill-build") == _auth.PERMISSION_NONE
    assert policy.get_permission("feishu:ou_1", "ZE-other-tool") == _auth.PERMISSION_READ  # 只影响该工具
    policy.set_permission("feishu:ou_1", "ZE-skill-build", _auth.PERMISSION_EDIT)
    assert policy.get_permission("feishu:ou_1", "ZE-skill-build") == _auth.PERMISSION_EDIT  # 显式授权优先


def test_none_default_also_blocks_model_scope(tmp_path):
    """回归:模型域门以前只挡 ban,新增 none 档后必须一并挡住,否则无权限用户能跑 AI。"""
    policy = _auth.PolicyStore(tmp_path / "policies.json")
    policy.set_tool_default("ZE-skill-build", _auth.PERMISSION_NONE)
    assert policy.get_model_scope_permission("feishu:ou_1", "ZE-skill-build", "image") is False
    policy.set_permission("feishu:ou_1", "ZE-skill-build", _auth.PERMISSION_READ)
    assert policy.get_model_scope_permission("feishu:ou_1", "ZE-skill-build", "image") is True


def test_tool_default_survives_reload(tmp_path):
    path = tmp_path / "policies.json"
    _auth.PolicyStore(path).set_tool_default("ZE-skill-deploy", _auth.PERMISSION_NONE)
    reloaded = _auth.PolicyStore(path)
    assert reloaded.tool_defaults() == {"ZE-skill-deploy": _auth.PERMISSION_NONE}
    assert reloaded.get_permission("feishu:ou_2", "ZE-skill-deploy") == _auth.PERMISSION_NONE


def test_access_request_approve_grants_permission(tmp_path):
    """审批即授权:批准后权限直接生效,不留「批了但没生效」的中间态。"""
    policy = _auth.PolicyStore(tmp_path / "policies.json")
    policy.set_tool_default("ZE-skill-build", _auth.PERMISSION_NONE)
    row = policy.request_access("feishu:ou_1", "ZE-skill-build", reason="需要看构建产物")
    assert row["state"] == _auth.ACCESS_REQUEST_PENDING
    assert [r["user_id"] for r in policy.list_access_requests("pending")] == ["feishu:ou_1"]
    done = policy.resolve_access_request(
        "feishu:ou_1", "ZE-skill-build", approve=True, decided_by="feishu:ou_admin")
    assert done["state"] == _auth.ACCESS_REQUEST_APPROVED
    assert done["decided_by"] == "feishu:ou_admin"
    assert policy.get_permission("feishu:ou_1", "ZE-skill-build") == _auth.PERMISSION_READ
    assert policy.list_access_requests("pending") == []


def test_access_request_reject_leaves_permission_untouched(tmp_path):
    policy = _auth.PolicyStore(tmp_path / "policies.json")
    policy.set_tool_default("ZE-skill-build", _auth.PERMISSION_NONE)
    policy.request_access("feishu:ou_1", "ZE-skill-build")
    done = policy.resolve_access_request("feishu:ou_1", "ZE-skill-build", approve=False)
    assert done["state"] == _auth.ACCESS_REQUEST_REJECTED
    assert policy.get_permission("feishu:ou_1", "ZE-skill-build") == _auth.PERMISSION_NONE
    with pytest.raises(ZeError) as e:                    # 已决的申请不能重复处理
        policy.resolve_access_request("feishu:ou_1", "ZE-skill-build", approve=True)
    assert e.value.code == "validation_failed"


def test_repeat_request_updates_single_row(tmp_path):
    """同一 用户×工具 重复提交只更新一行,管理台待办不会被刷屏。"""
    policy = _auth.PolicyStore(tmp_path / "policies.json")
    policy.set_tool_default("ZE-tool-a", _auth.PERMISSION_NONE)
    policy.request_access("feishu:ou_1", "ZE-tool-a", reason="第一次")
    policy.request_access("feishu:ou_1", "ZE-tool-a", requested=_auth.PERMISSION_EDIT, reason="第二次")
    rows = policy.list_access_requests()
    assert len(rows) == 1
    assert rows[0]["requested"] == _auth.PERMISSION_EDIT
    assert rows[0]["reason"] == "第二次"


def test_banned_user_and_sufficient_permission_cannot_request(tmp_path):
    policy = _auth.PolicyStore(tmp_path / "policies.json")
    policy.set_permission("feishu:ou_bad", "ZE-tool-a", _auth.PERMISSION_BAN)
    with pytest.raises(ZeError) as e:
        policy.request_access("feishu:ou_bad", "ZE-tool-a")
    assert e.value.code == "validation_failed"
    policy.set_permission("feishu:ou_ok", "ZE-tool-a", _auth.PERMISSION_EDIT)
    with pytest.raises(ZeError) as e:                    # 已有 edit,再申请 read 无意义
        policy.request_access("feishu:ou_ok", "ZE-tool-a")
    assert e.value.code == "validation_failed"
    with pytest.raises(ZeError) as e:                    # 不能申请 none/ban 这种非放行档
        policy.request_access("feishu:ou_new", "ZE-tool-a", requested=_auth.PERMISSION_BAN)
    assert e.value.code == "validation_failed"


def test_resolve_unknown_request_is_not_found(tmp_path):
    policy = _auth.PolicyStore(tmp_path / "policies.json")
    with pytest.raises(ZeError) as e:
        policy.resolve_access_request("feishu:ou_x", "ZE-tool-a", approve=True)
    assert e.value.code == "not_found"


def test_super_admin_bypasses_none_default(tmp_path):
    """本地给自己开权限的兜底路径:超管不受工具默认档影响。"""
    policy = _auth.PolicyStore(tmp_path / "policies.json")
    policy.set_tool_default("ZE-skill-build", _auth.PERMISSION_NONE)
    admin = sorted(_auth.super_admin_users())
    if not admin:
        pytest.skip("no super admin configured in this environment")
    assert policy.get_permission(admin[0], "ZE-skill-build") == _auth.PERMISSION_EDIT


def test_all_policies_exposes_defaults_and_requests(tmp_path):
    """管理台一次读全:默认档 + 用户授权 + 申请队列。"""
    policy = _auth.PolicyStore(tmp_path / "policies.json")
    policy.set_tool_default("ZE-tool-a", _auth.PERMISSION_NONE)
    policy.request_access("feishu:ou_1", "ZE-tool-a", reason="申请理由")
    snap = policy.all_policies()
    assert snap["schema_version"] == "1.1.0"
    assert snap["tool_defaults"] == {"ZE-tool-a": _auth.PERMISSION_NONE}
    assert [r["reason"] for r in snap["access_requests"]] == ["申请理由"]


def test_legacy_policy_file_without_new_fields_still_loads(tmp_path):
    """存量 1.0.0 文件(无 tool_defaults / access_requests)照常读,行为不变。"""
    path = tmp_path / "policies.json"
    path.write_text(json.dumps({
        "schema_version": "1.0.0",
        "default_permission": "read",
        "users": {"feishu:ou_1": {"tool_permissions": {"ZE-tool-a": "edit"}}},
    }), encoding="utf-8")
    policy = _auth.PolicyStore(path)
    assert policy.get_permission("feishu:ou_1", "ZE-tool-a") == _auth.PERMISSION_EDIT
    assert policy.get_permission("feishu:ou_1", "ZE-tool-b") == _auth.PERMISSION_READ
    assert policy.tool_defaults() == {}
    assert policy.list_access_requests() == []


def test_clear_permission_returns_user_to_tool_default(tmp_path):
    """撤回显式授权 = 回到跟随默认档,不是钉死在某一档(否则授权是单向门)。"""
    policy = _auth.PolicyStore(tmp_path / "policies.json")
    policy.set_tool_default("ZE-tool-a", _auth.PERMISSION_NONE)
    policy.set_permission("feishu:ou_1", "ZE-tool-a", _auth.PERMISSION_EDIT)
    assert policy.get_permission("feishu:ou_1", "ZE-tool-a") == _auth.PERMISSION_EDIT
    assert policy.clear_permission("feishu:ou_1", "ZE-tool-a") is True
    assert policy.get_permission("feishu:ou_1", "ZE-tool-a") == _auth.PERMISSION_NONE
    # 撤回后再改默认档,这个人跟着变 —— 这正是「跟随」的意思
    policy.set_tool_default("ZE-tool-a", _auth.PERMISSION_READ)
    assert policy.get_permission("feishu:ou_1", "ZE-tool-a") == _auth.PERMISSION_READ


def test_clear_permission_is_idempotent_and_keeps_other_tools(tmp_path):
    policy = _auth.PolicyStore(tmp_path / "policies.json")
    policy.set_permission("feishu:ou_1", "ZE-tool-a", _auth.PERMISSION_EDIT)
    policy.set_permission("feishu:ou_1", "ZE-tool-b", _auth.PERMISSION_READ)
    assert policy.clear_permission("feishu:ou_1", "ZE-tool-a") is True
    assert policy.clear_permission("feishu:ou_1", "ZE-tool-a") is False   # 再撤一次:无事发生
    assert policy.user_permissions("feishu:ou_1") == {"ZE-tool-b": _auth.PERMISSION_READ}


def test_clear_permission_survives_reload(tmp_path):
    path = tmp_path / "policies.json"
    p1 = _auth.PolicyStore(path)
    p1.set_permission("feishu:ou_1", "ZE-tool-a", _auth.PERMISSION_EDIT)
    p1.clear_permission("feishu:ou_1", "ZE-tool-a")
    assert _auth.PolicyStore(path).user_permissions("feishu:ou_1") == {}


def test_clear_permission_validates_args(tmp_path):
    policy = _auth.PolicyStore(tmp_path / "policies.json")
    for args in (("", "ZE-tool-a"), ("feishu:ou_1", "")):
        with pytest.raises(ZeError) as e:
            policy.clear_permission(*args)
        assert e.value.code == "validation_failed"


# ── session 解析缓存:快了不算数,语义不变才算数 ────────────────────────────────

def test_外部进程改了文件_缓存必须失效(tmp_path, tmp_store):
    """缓存靠 (mtime_ns, size, inode) 失效。别的进程登出/登录后,这边必须看得见。

    用 mtime **纳秒**而不是秒 —— 秒级粒度下「同一秒内先写后读」会读到陈旧解析。
    """
    path = tmp_path / "sessions.json"
    user = tmp_store.register("alice", "hunter222")
    writer = SessionManager(ttl_sec=3600, path=path)
    token = writer.login(user)

    reader = SessionManager(ttl_sec=3600, path=path)
    assert reader.get_user_id(token) == "alice"      # 预热缓存

    writer.logout(token)                              # 另一个「进程」把它登出
    assert reader.get_user_id(token) is None, "文件变了,缓存却没失效"


def test_缓存命中不让过期session复活(tmp_path, tmp_store):
    """缓存只跳过「读文件+解析」,过期判定不在那里 ——
    get_user_id 自己逐 token 判 expires_at。这条是缓存安全的前提。"""
    path = tmp_path / "sessions.json"
    user = tmp_store.register("bob", "hunter222")
    sm = SessionManager(ttl_sec=1, path=path)
    token = sm.login(user)
    assert sm.get_user_id(token) == "bob"             # 预热缓存,文件此后不变
    time.sleep(1.1)
    assert sm.get_user_id(token) is None, "缓存命中时过期 session 复活了"


def test_同进程写入后自己立刻读得到(tmp_path, tmp_store):
    """自己写的那一次不该让缓存作废,也不该读到旧的。"""
    path = tmp_path / "sessions.json"
    a = tmp_store.register("carol", "hunter222")
    sm = SessionManager(ttl_sec=3600, path=path)
    t1 = sm.login(a)
    assert sm.get_user_id(t1) == "carol"
    t2 = sm.login(a)                                   # 再签一个
    assert sm.get_user_id(t2) == "carol"
    assert sm.get_user_id(t1) == "carol"
