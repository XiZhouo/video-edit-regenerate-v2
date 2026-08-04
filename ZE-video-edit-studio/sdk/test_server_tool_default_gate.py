"""工具默认档在服务端的放行判定(server._effective_permission / _require_tool_access)。

用户令:标准 skill 默认无权限、须申请;本地给自己开权限。默认档存 PolicyStore,
服务端必须认它 —— 否则「默认无权限」只是管理台上的一个摆设字段。

覆盖:①没设默认档时行为不变(登录即 edit,存量部署不被这次改动锁死);②设了默认档才夺权;
③用户显式授权 > 默认档;④none 被拒但可申请、ban 被拒且不可申请(文案/上下文要能区分);
⑤超管不受默认档影响(本地自开权限的兜底路径);⑥禁用账号一律 ban。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _d in (
    _ROOT,
    _ROOT / "01-core" / "01-data-structure" / "01-document",
    _ROOT / "01-core" / "01-data-structure" / "02-entity",
    _ROOT / "01-core" / "_protocol",
):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import server  # noqa: E402
from auth import (  # noqa: E402
    PERMISSION_BAN, PERMISSION_EDIT, PERMISSION_NONE, PERMISSION_READ,
    PolicyStore, USER_STATUS_ACTIVE, USER_STATUS_DISABLED,
)
from entity import Actor, ZeError  # noqa: E402

TOOL = "ZE-skill-build"


class _AuditSpy:
    def append(self, event_type: str, **details):
        return {}


def _fake_repo(tmp_path, monkeypatch, users: dict, *, declared_default: str | None = None):
    monkeypatch.setenv("ZE_SYSTEM_AUTH_DIR", str(tmp_path))
    policy = PolicyStore(tmp_path / "policies.json")
    cfg = {"ze.auth.tool_default": declared_default} if declared_default else {}
    repo = SimpleNamespace(
        config=SimpleNamespace(get=lambda k, d=None: cfg.get(k, d)),
        policy_store=policy,
        user_store=SimpleNamespace(get=lambda uid: users.get(uid),
                                   list_users=lambda: list(users.values())),
        auth_audit=_AuditSpy(),
        tool_id=TOOL,
    )
    monkeypatch.setattr(server, "_repo", repo)
    return policy


def _user(user_id: str, status: str = USER_STATUS_ACTIVE):
    return SimpleNamespace(user_id=user_id, status=status,
                           display_name=user_id, email=f"{user_id}@example.com")


def test_no_tool_default_keeps_legacy_login_means_edit(tmp_path, monkeypatch):
    """没设默认档 → 行为与改动前一致。存量部署不会因为这次改动被锁死。"""
    u = _user("feishu:ou_1")
    _fake_repo(tmp_path, monkeypatch, {u.user_id: u})
    assert server._effective_permission(u) == PERMISSION_EDIT
    assert server._require_tool_access(Actor(user_id=u.user_id)) is u


def test_tool_default_none_denies_but_marks_requestable(tmp_path, monkeypatch):
    u = _user("feishu:ou_1")
    policy = _fake_repo(tmp_path, monkeypatch, {u.user_id: u})
    policy.set_tool_default(TOOL, PERMISSION_NONE)
    assert server._effective_permission(u) == PERMISSION_NONE
    with pytest.raises(ZeError) as ei:
        server._require_tool_access(Actor(user_id=u.user_id))
    assert ei.value.code == "acl_denied"
    assert (ei.value.context or {}).get("can_request") is True


def test_ban_is_denied_and_not_requestable(tmp_path, monkeypatch):
    """ban 与 none 都拒,但前端要能分辨:一个申请有用,一个申请没用。"""
    u = _user("feishu:ou_bad")
    policy = _fake_repo(tmp_path, monkeypatch, {u.user_id: u})
    policy.set_permission(u.user_id, TOOL, PERMISSION_BAN)
    with pytest.raises(ZeError) as ei:
        server._require_tool_access(Actor(user_id=u.user_id))
    assert ei.value.code == "acl_denied"
    assert (ei.value.context or {}).get("can_request") is False


def test_explicit_grant_beats_tool_default(tmp_path, monkeypatch):
    u = _user("feishu:ou_1")
    policy = _fake_repo(tmp_path, monkeypatch, {u.user_id: u})
    policy.set_tool_default(TOOL, PERMISSION_NONE)
    policy.set_permission(u.user_id, TOOL, PERMISSION_READ)
    assert server._effective_permission(u) == PERMISSION_READ
    assert server._require_tool_access(Actor(user_id=u.user_id)) is u
    with pytest.raises(ZeError) as ei:          # read 够进门,不够写
        server._require_edit(Actor(user_id=u.user_id))
    assert ei.value.code == "read_only_violation"


def test_tool_default_read_is_distinguishable_from_unset(tmp_path, monkeypatch):
    """显式设成 read ≠ 没设过 —— 后者按存量语义给 edit。"""
    u = _user("feishu:ou_1")
    policy = _fake_repo(tmp_path, monkeypatch, {u.user_id: u})
    policy.set_tool_default(TOOL, PERMISSION_READ)
    assert server._effective_permission(u) == PERMISSION_READ
    policy.set_tool_default("ZE-other-tool", PERMISSION_NONE)   # 别的工具设 none 不影响本工具
    assert server._effective_permission(u) == PERMISSION_READ


def test_super_admin_unaffected_by_tool_default(tmp_path, monkeypatch):
    admin = _user("feishu:ou_admin")
    policy = _fake_repo(tmp_path, monkeypatch, {admin.user_id: admin})
    policy.set_tool_default(TOOL, PERMISSION_NONE)
    monkeypatch.setenv("ZE_SUPER_ADMIN_USERS", admin.user_id)
    assert server._effective_permission(admin) == PERMISSION_EDIT


def test_disabled_account_is_banned_regardless_of_default(tmp_path, monkeypatch):
    u = _user("feishu:ou_off", status=USER_STATUS_DISABLED)
    policy = _fake_repo(tmp_path, monkeypatch, {u.user_id: u})
    policy.set_tool_default(TOOL, PERMISSION_EDIT)
    assert server._effective_permission(u) == PERMISSION_BAN
    with pytest.raises(ZeError) as ei:
        server._require_tool_access(Actor(user_id=u.user_id))
    assert (ei.value.context or {}).get("can_request") is False


# ---------------------------------------------------------------
# 自助申请入口本身必须对「无权限的人」开着 —— 否则整套 none 档 + 申请流是死的
# ---------------------------------------------------------------

class _Req:
    """最小 Request 替身:端点只用到 await request.json()。"""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_none_tier_user_can_actually_submit_a_request(tmp_path, monkeypatch):
    """回归:端点曾用 _require_tool_access 把门,而那道门恰好挡的就是 none 档 ——
    提示语写着「可在工具内提交申请」,申请却永远递不进队列。"""
    u = _user("feishu:ou_1")
    policy = _fake_repo(tmp_path, monkeypatch, {u.user_id: u})
    policy.set_tool_default(TOOL, PERMISSION_NONE)
    assert server._effective_permission(u) == PERMISSION_NONE

    out = _run(server.account_permission_request(
        _Req({"permission": PERMISSION_READ, "reason": "需要看构建产物"}),
        actor=Actor(user_id=u.user_id),
    ))
    assert out["status"] == "recorded"
    queued = policy.list_access_requests("pending")
    assert [r["user_id"] for r in queued] == [u.user_id]
    assert queued[0]["reason"] == "需要看构建产物"


def test_request_endpoint_still_requires_login(tmp_path, monkeypatch):
    """开的是「无权限可申请」,不是「谁都能匿名申请」。"""
    _fake_repo(tmp_path, monkeypatch, {})
    with pytest.raises(ZeError) as ei:
        _run(server.account_permission_request(_Req({}), actor=Actor(user_id="")))
    assert ei.value.code == "auth_required"


def test_request_endpoint_rejects_unknown_account(tmp_path, monkeypatch):
    _fake_repo(tmp_path, monkeypatch, {})
    with pytest.raises(ZeError) as ei:
        _run(server.account_permission_request(_Req({}), actor=Actor(user_id="feishu:ghost")))
    assert ei.value.code == "auth_required"


def test_already_granted_user_gets_told_so_not_queued(tmp_path, monkeypatch):
    u = _user("feishu:ou_ok")
    policy = _fake_repo(tmp_path, monkeypatch, {u.user_id: u})
    policy.set_permission(u.user_id, TOOL, PERMISSION_EDIT)
    out = _run(server.account_permission_request(_Req({}), actor=Actor(user_id=u.user_id)))
    assert out["status"] == "already_granted"
    assert policy.list_access_requests("pending") == []


def test_banned_user_request_is_audited_but_not_queued(tmp_path, monkeypatch):
    """ban 也能递(端点不再把门),但队列按策略拒收 —— 待办里不该出现注定驳回的行。"""
    u = _user("feishu:ou_bad")
    policy = _fake_repo(tmp_path, monkeypatch, {u.user_id: u})
    policy.set_permission(u.user_id, TOOL, PERMISSION_BAN)
    out = _run(server.account_permission_request(_Req({}), actor=Actor(user_id=u.user_id)))
    assert out["status"] == "recorded"          # 意图照记(审计)
    assert policy.list_access_requests("pending") == []   # 但不进待办


# ── 声明式默认档(部署声明,不靠人记得去管理台点)──────────────────────────

def test_declared_none_closes_a_fresh_deployment(tmp_path, monkeypatch):
    """标准 skill 部署到公网:**新部署一上来就该是关的**。

    改之前兜底硬编码 return PERMISSION_EDIT —— 谁登录都能编辑,只能靠人记得去管理台设 none。
    「安全默认」不该依赖记性,该由部署声明。
    """
    u = _user("feishu:ou_stranger")
    _fake_repo(tmp_path, monkeypatch, {u.user_id: u}, declared_default=PERMISSION_NONE)
    assert server._effective_permission(u) == PERMISSION_NONE
    with pytest.raises(ZeError) as ei:
        server._require_tool_access(Actor(user_id=u.user_id))
    assert ei.value.code == "acl_denied"


def test_admin_tool_default_still_overrides_the_declaration(tmp_path, monkeypatch):
    """管理台设过的档位优先于部署声明 —— 声明是默认值,不是天花板。"""
    u = _user("feishu:ou_1")
    policy = _fake_repo(tmp_path, monkeypatch, {u.user_id: u}, declared_default=PERMISSION_NONE)
    policy.set_tool_default(TOOL, PERMISSION_READ)
    assert server._effective_permission(u) == PERMISSION_READ


def test_missing_config_falls_back_to_legacy_edit(tmp_path, monkeypatch):
    """存量部署没有这个 knob:行为必须与改动前逐字一致,不能因为加了声明就把人锁死。"""
    u = _user("feishu:ou_1")
    _fake_repo(tmp_path, monkeypatch, {u.user_id: u})       # 不给 declared_default
    assert server._effective_permission(u) == PERMISSION_EDIT


def test_unknown_declared_value_falls_back_not_locks_out(tmp_path, monkeypatch):
    """声明写错(拼错/旧值)不该把所有人关在门外 —— 那是把配置错误升级成停服。"""
    u = _user("feishu:ou_1")
    _fake_repo(tmp_path, monkeypatch, {u.user_id: u}, declared_default="readonly")
    assert server._effective_permission(u) == PERMISSION_EDIT
