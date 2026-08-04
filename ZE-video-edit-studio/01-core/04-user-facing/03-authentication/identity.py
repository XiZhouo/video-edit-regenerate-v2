"""identity.py — HTTP 请求 → Actor 身份桥(轻量,纯函数,不依赖 FastAPI)。

把请求里的 session token(`Authorization: Bearer` / cookie / `?token=`)解析成
`Actor`。无 token / 失效 / 用户不存在 → 匿名 `Actor()`(user_id="")。

设计:
  - 纯函数,只依赖 auth.py 的 SessionManager/UserStore + entity.Actor,不 import FastAPI。
  - server 在每个需要身份的路由调 `resolve_actor(...)`,把结果当作该请求的 actor。
  - 匿名兜底配合 `CommandRegistry.enforce_login` + server ACL 校验拦未登录/越权写。
"""

from __future__ import annotations

import sys
from pathlib import Path

# v3 平铺布局:把自身目录(03-authentication)与 02-entity 注入 sys.path,
# 使 `from auth import ...` / `from entity import ...` 在任意 import 入口都可解析。
_AUTH_DIR = Path(__file__).resolve().parent
_ENTITY_DIR = _AUTH_DIR.parent.parent / "01-data-structure" / "02-entity"
for _d in (_AUTH_DIR, _ENTITY_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from entity import Actor  # noqa: E402
from auth import UserStore, SessionManager, build_actor  # noqa: E402


def extract_bearer_token(authorization: str | None) -> str:
    """从 `Authorization` 头取 Bearer token;非 Bearer / 缺失 → 空串。"""
    if not authorization:
        return ""
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def resolve_actor(
    *,
    authorization: str | None = None,
    cookie_token: str | None = None,
    query_token: str | None = None,
    user_store: UserStore,
    session_mgr: SessionManager,
) -> Actor:
    """请求 → Actor。token 优先级:`Authorization: Bearer` > 共享 cookie > query。

    任一环节失败(无 token / token 失效 / 用户已不存在)→ 匿名 `Actor()`(user_id="")。
    不抛错 —— 鉴权决策留给 server 层(匿名读放行公共项目,匿名写由 enforce_login 拦)。
    """
    token = (
        extract_bearer_token(authorization)
        or (cookie_token or "").strip()
        or (query_token or "").strip()
    )
    if not token:
        return Actor(user_id="")
    user_id = session_mgr.get_user_id(token)
    if not user_id:
        return Actor(user_id="")
    user = user_store.get(user_id)
    if user is None:
        return Actor(user_id="")
    return build_actor(user, source="user")


__all__ = ["resolve_actor", "extract_bearer_token"]
