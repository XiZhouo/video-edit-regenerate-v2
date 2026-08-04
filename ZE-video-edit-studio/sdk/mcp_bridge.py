"""sdk/mcp_bridge.py — MCP-over-HTTP 适配层(ZE 命令总线的第四个 transport).

图景:ZE 早已有「AI 的左手」—— `scripts/ze_command.py`(CLI 镜像)与 server GET 路由
都调同一 `CommandRegistry.dispatch()` + 同一 `sdk/introspect.py` reader。本模块再加一个
**标准协议端点**:把同一套命令/introspection 经 MCP(Model Context Protocol)暴露给外部
agent(Claude Code / Codex / Cursor),agent 零适配即可发现工具、调用 —— 协议即接口。

与 `sdk/introspect.py` 同层、同角色(都是把 AI 最低接口暴露出去),故住在 sdk/ 聚合层而非
core 叶子:它 `from sdk import ...` 复用 core,放进 core 叶子会造成「叶子反向 import sdk」的
层级倒置。

红线 / 复用(不重写):
  - tools/list  ← `introspect.list_commands`(过滤 `"ai" in sources_allowed`)
  - tools/call  → `ctx.commit.execute(cmd_reg.dispatch(..., source="ai"))`,与 HTTP/CLI 同回执
  - resources   ← `describe_context / list_entities / query_oplog`
  - source="ai" → dispatch 自动过 `AiGovernor.guard()`(成本/安全护栏,免费继承)
  - MCP 写工具公开 `_idempotency_key`，与 HTTP/CLI 共用 request ledger；JSON-RPC `id`
    只标识协议消息，绝不能代替业务幂等键

传输:stateless MCP-over-HTTP(JSON-RPC 2.0 over POST),零新依赖(MCP 的 HTTP 传输本质即
JSON-RPC 请求-响应;tools-only 无需 SSE 会话)。纯协议核心 `handle_mcp_message` 不依赖
FastAPI,可离线单测;`register_mcp_routes` 只做 actor 解析 + 包壳。

鉴权(Hybrid,secure-by-default):
  - 默认任何 HTTP MCP 调用都必须带 session token,解析成真 Actor,再过工具级 PolicyStore
    + 项目级 owner/collaborator ACL(镜像 server `_require_member`)。
  - 仅显式 `ZE_MCP_ALLOW_LOOPBACK_TRUST=1`(或 legacy `ZE_MCP_REQUIRE_TOKEN=0`)时,直连
    loopback 且无代理头才给保留账号 `__mcp__` 本机全权。经反代/非 loopback 仍强制 token。
"""

from __future__ import annotations

import base64
import json
import os
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sdk.asset_access import asset_ref_for_read_locked, durable_asset_access

from sdk import (  # 复用 core(同 introspect.py 的接法)
    Actor, ZeError,
    resolve_actor,
    describe_context, list_commands, describe_command, list_entities, query_oplog,
    read_manual,
    compile_guided_tours, find_tour, normalize_adhoc_steps, adhoc_tour_mode,
    guide_step_frame, guide_end_frame,
    PERMISSION_EDIT, PERMISSION_BAN, USER_STATUS_DISABLED,
    credential_context,
    PROVIDER_GITHUB_REPO, META_REPO_OWNER, META_REPO_NAME, META_REPO_REF,
    github_read_path,
)
from sdk._paths import ROOT
from sdk.persistence import commit_execution_payload, pending_confirmation_frame
from sdk.error_projection import (
    commit_outcome_unknown,
    is_commit_authority_error,
    public_commit_authority_context,
    public_error_message,
)

# MCP 协议版本(支持的集合;客户端请求已知版本则回显,否则回服务端默认)。
MCP_DEFAULT_PROTOCOL = "2025-06-18"
_SUPPORTED_PROTOCOLS = {"2025-06-18", "2025-03-26", "2024-11-05"}
SERVER_NAME = "ze-skill"
# 本机全权保留账号(非空 → 过 cmd_reg enforce_login;不经 HTTP ACL)。
MCP_LOCAL_USER_ID = "__mcp__"

# JSON-RPC 标准错误码 + 应用级鉴权码(-32001,区别于 -32603「服务端 bug」)。
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_UNAUTHORIZED = -32001
_AUTH_CODES = {"auth_required", "acl_denied", "read_only_violation", "model_scope_denied"}


def _rpc_code_for(ze_code: str) -> int:
    return _UNAUTHORIZED if ze_code in _AUTH_CODES else _INTERNAL_ERROR


def _http_status_for(ze_code: str) -> int:
    if ze_code == "auth_required":
        return 401
    if ze_code in ("acl_denied", "read_only_violation", "model_scope_denied"):
        return 403
    if ze_code in (
        "project_busy", "project_generation_stale", "project_recovery_required",
        "idempotency_in_progress", "idempotency_outcome_unknown",
    ):
        return 409
    return 200


def _commit_authority_error_body(error: BaseException) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": getattr(error, "code", "commit_authority_failed"),
        "message": public_error_message(error),
    }
    context = public_commit_authority_context(error)
    if context:
        body["context"] = context
    return body


# ==============================================================
# 工具名 ↔ command_id 变换
# ==============================================================
# 命令 id 含点(`entity.add`);部分 MCP 客户端工具名限 [A-Za-z0-9_-]。统一 `.`→`-`;
# 反解时按候选逐一回 registry.has 验证,兼容 id 里本就有的 `_`/`-`。

def command_to_tool_name(command_id: str) -> str:
    return command_id.replace(".", "-")


def resolve_command_id(cmd_reg: Any, tool_name: str) -> str | None:
    for cand in (tool_name, tool_name.replace("-", "."), tool_name.replace("__", ".")):
        if cmd_reg.has(cand):
            return cand
    return None


# ze 元工具（读 introspection + 取消本人 pending + 列项目）；批准只存在于网页人工通道。
_META_TOOLS = {
    "ze_describe": "返回本工具对象图(实体类型/命令/编辑器/管线/ai_flow)。Context.Describe。",
    "ze_entities": "列实体(id/name/version)。可选 arg: type;include_data=true 追加实体完整 data(过项目 ACL)。",
    "ze_oplog": "查操作日志(审计轨迹 + 链完整性)。可选 arg: type/id/source/limit。",
    "ze_manual": "读本工具人类说明书(markdown 全文)。大文档可选 arg: offset/limit 分页。",
    "ze_read_asset": ("按 sha256 读一个资产(过项目 ACL,只对本项目本地 blob 或被 canonical "
                      "Document 引用的共享 blob 放行)。mode=meta 返元数据 / text 返文本内容 / "
                      "describe 让你直接看到该图片。arg: sha256, mode。"),
    "ze_file": ("读当前账号关联的 GitHub 仓库里的一个相对路径:传目录返条目列表,传文件返文本内容"
                "(过项目 ACL;令牌只服务端用,只读、从不写 GitHub)。arg: path(相对仓库根,空=根目录)。"),
    "ze_list_projects": "列仓库里的 project(id/name/owner)。skill 级,无需 project。",
    "ze_cancel": "取消一个待确认命令。arg: token。",
    "ze_guide": ("驱动一段引导(引导精灵/系统 3a):传 tour_id 跑 ze_describe.guided_tours 里的某条,"
                 "或传 steps 自定义。指路步(say/point/highlight/ask/wait)只流式给前端;代操作(act)步"
                 "server 端过 ACL/确认闸门 dispatch,高危命令进待批准,你无法自批。arg: tour_id 或 steps。"),
}

# 只读元工具(不需 EDIT 权限;写工具默认 need_edit)。
_READ_META_TOOLS = ("ze_describe", "ze_entities", "ze_oplog", "ze_manual", "ze_read_asset", "ze_file")

# ze_read_asset：文本模式可 decode 的 mime + 文本截断上限(防超大文本撑爆通道)。
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = frozenset({
    "application/json", "application/xml", "application/xhtml+xml",
    "application/javascript", "application/x-yaml", "application/yaml",
    "application/csv", "application/toml", "image/svg+xml",
})
_READ_ASSET_TEXT_MAX = 200_000        # decode 后字符上限
# describe 走 MCP 原生 image content block(base64);过大不内联(避免撑爆响应)。
_READ_ASSET_IMAGE_MAX_BYTES = 8 * 1024 * 1024


def _is_text_mime(mime: str) -> bool:
    mime = (mime or "").split(";", 1)[0].strip().lower()
    return mime in _TEXT_MIME_EXACT or any(mime.startswith(p) for p in _TEXT_MIME_PREFIXES)


# ==============================================================
# 鉴权(Hybrid)
# ==============================================================

@dataclass
class McpActorCtx:
    actor: Actor
    enforce_acl: bool          # True=token 模式(过工具/项目 ACL);False=本机全权
    project_hint: str = ""     # ?project= 查询参数(tool arg 里的 _project 优先)
    project_generation: int = 0  # 非 0 时把长任务钉在 URL 创建时的 project generation
    turn_id: str = ""           # §7.4 P0-E:本次回调属于哪一轮助手对话(空=非助手轮)


# 反向代理转发头:出现任一即说明请求经代理转发,request.client.host(=代理回环地址)
# 不可信为「本机」—— 必须强制 token,否则反代后端(nginx/caddy 默认注入这些头)会让所有
# 远程调用看起来都是 loopback,绕过 ACL 拿 __mcp__ 全权(安全审计 CRITICAL)。
_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "forwarded")


def _is_loopback(client_host: str | None) -> bool:
    # 对端未知(client=None)按非 loopback 处理(安全默认:宁可要 token)。
    return client_host in ("127.0.0.1", "::1", "localhost")


_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def _env_flag(name: str) -> str:
    return str(os.environ.get(name, "")).strip().lower()


def allow_loopback_trust() -> bool:
    """Dev escape hatch for local MCP clients.

    Default is token-required even on loopback. `ZE_MCP_REQUIRE_TOKEN=0` remains
    a legacy explicit opt-out, but the clearer knob is `ZE_MCP_ALLOW_LOOPBACK_TRUST=1`.

    部署态(start.sh 恒设 `ZE_SERVER_MODE=1`)硬拒 loopback-trust:忽略
    `ZE_MCP_ALLOW_LOOPBACK_TRUST` / legacy `ZE_MCP_REQUIRE_TOKEN=0`。反代注入 loopback 头
    即可让远程请求冒充本机(见 _PROXY_HEADERS),部署面绝不给本机全权豁免,一律要 token。
    """
    if _env_flag("ZE_SERVER_MODE") in _TRUTHY:
        return False
    if _env_flag("ZE_MCP_ALLOW_LOOPBACK_TRUST") in _TRUTHY:
        return True
    return _env_flag("ZE_MCP_REQUIRE_TOKEN") in _FALSEY


def require_token_mode() -> bool:
    """部署级策略:安全默认 token;显式本机 opt-out 或非 loopback bind 覆盖。

    注:ZE start.sh 恒 `uvicorn --host 0.0.0.0`,故无法用「服务绑 loopback」当安全信号;
    真实威胁是反代(见 _PROXY_HEADERS),由 _resolve_actor 逐请求叠加判定。
    """
    # 部署态硬门:独立于 allow_loopback_trust() 也断言一次(纵深),即便后者被重构也不放松。
    if _env_flag("ZE_SERVER_MODE") in _TRUTHY:
        return True
    if _env_flag("ZE_MCP_REQUIRE_TOKEN") in _TRUTHY:
        return True
    bind = str(os.environ.get("ZE_MCP_BIND", "")).strip()
    if bool(bind) and bind not in ("127.0.0.1", "::1", "localhost"):
        return True
    return not allow_loopback_trust()


def _has_proxy_headers(headers: Any) -> bool:
    keys = headers.keys() if hasattr(headers, "keys") else headers
    return any(str(h).lower() in _PROXY_HEADERS for h in keys)


def _should_use_local_actor(client_host: str | None, headers: Any) -> bool:
    return (
        allow_loopback_trust()
        and not require_token_mode()
        and _is_loopback(client_host)
        and not _has_proxy_headers(headers)
    )


def _resolve_http_actor_context(request: Any, repo: Any) -> McpActorCtx:
    project_hint = request.query_params.get("project", "") or ""
    turn_id = request.query_params.get("turn", "") or ""
    generation_raw = request.query_params.get("generation", "") or ""
    try:
        project_generation = int(generation_raw) if generation_raw else 0
    except (TypeError, ValueError) as exc:
        raise ZeError("validation_failed", "invalid MCP project generation") from exc
    if project_generation < 0:
        raise ZeError("validation_failed", "invalid MCP project generation")
    client_host = request.client.host if request.client else None
    if _should_use_local_actor(client_host, request.headers):
        return McpActorCtx(actor=Actor(user_id=MCP_LOCAL_USER_ID, source="ai"),
                           enforce_acl=False, project_hint=project_hint,
                           project_generation=project_generation, turn_id=turn_id)
    actor = resolve_actor(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("ze_session"),
        query_token=request.query_params.get("token"),
        user_store=repo.user_store, session_mgr=repo.session_mgr,
    )
    if not getattr(actor, "user_id", ""):
        raise ZeError("auth_required", "MCP requires a session token")
    return McpActorCtx(
        actor=actor,
        enforce_acl=True,
        project_hint=project_hint,
        project_generation=project_generation,
        turn_id=turn_id,
    )


def _require_tool_access(repo: Any, actor: Actor, *, need_edit: bool) -> None:
    """镜像 server `_require_tool_access`/`_require_edit`:登录 + 未 ban (+ 写需 EDIT)。"""
    if not getattr(actor, "user_id", ""):
        raise ZeError("auth_required", "MCP token 模式需要登录身份")
    user = repo.user_store.get(actor.user_id)
    if user is None:
        raise ZeError("auth_required", "MCP token 无对应用户")
    if user.status == USER_STATUS_DISABLED:
        raise ZeError("acl_denied", "当前用户已禁用")
    permission = repo.policy_store.get_permission(user.user_id, repo.tool_id)
    if permission == PERMISSION_BAN:
        raise ZeError("acl_denied", "当前用户无权访问此 ZE 工具")
    if need_edit and permission != PERMISSION_EDIT:
        raise ZeError("read_only_violation", "当前用户是只读权限,不能经 MCP 写")


def _can_access(acl: dict, user_id: str) -> bool:
    owner = acl.get("owner", "")
    if not owner:
        return True
    return user_id == owner or user_id in (acl.get("collaborators") or [])


def _member_context(repo: Any, project_id: str, actor_ctx: McpActorCtx, *, need_edit: bool):
    """定位 project 的 Context。token 模式镜像 server `_member_context_for` 过两级 ACL;
    本机全权模式直接 get_or_load(等同 CLI)。"""
    if actor_ctx.enforce_acl:
        _require_tool_access(repo, actor_ctx.actor, need_edit=need_edit)
    ctx = repo.get_or_load(project_id)
    expected_generation = (
        actor_ctx.project_generation
        if project_id == actor_ctx.project_hint
        else 0
    )
    if expected_generation and getattr(ctx, "generation", 0) != expected_generation:
        raise ZeError(
            "project_generation_stale",
            "MCP task belongs to an obsolete project generation",
            context={
                "project_id": project_id,
                "expected_generation": expected_generation,
                "current_generation": getattr(ctx, "generation", 0),
            },
        )
    if actor_ctx.enforce_acl and not _can_access(ctx.doc.acl, actor_ctx.actor.user_id):
        raise ZeError("acl_denied", f"no access to project {project_id!r}",
                      context={"project_id": project_id})
    return ctx


def _resolve_project_id(repo: Any, requested: str) -> str:
    """显式 project 优先;否则仓库唯一时自动选;多/零则报错(镜像 ze_command._resolve_project)。"""
    ids = repo.list_project_ids()
    if requested:
        if requested not in ids:
            raise ZeError("project_not_found", f"no project {requested!r}",
                          context={"available": ids})
        return requested
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise ZeError("project_not_found", "仓库没有任何 project(先经 web 新建)", context={})
    raise ZeError("validation_failed",
                  f"仓库有多个 project,请在 MCP URL 加 ?project=<id> 或工具 arg 传 _project: {ids}",
                  context={"available": ids})


# ==============================================================
# 工具目录 / 调用
# ==============================================================

def _command_tools(cmd_reg: Any) -> list[dict[str, Any]]:
    """命令工具:过滤 `"ai" in sources_allowed`(MCP 以 source='ai' 调用)。"""
    tools: list[dict[str, Any]] = []
    for brief in list_commands(cmd_reg):
        if "ai" not in (brief.get("sources_allowed") or []):
            continue
        params = json.loads(json.dumps(
            brief.get("params_schema") or {"type": "object", "properties": {}}
        ))
        if params.get("type") == "object":
            properties = dict(params.get("properties") or {})
            properties["_idempotency_key"] = {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": (
                    "业务幂等键；网络重试必须复用同一个值，不能用 JSON-RPC id 代替"
                ),
            }
            params["properties"] = properties
        desc = brief.get("description") or brief.get("label") or brief["id"]
        if brief.get("requires_confirm"):
            desc += "（调用后进入待批准；只能由用户在网页界面人工批准，MCP 无批准能力）"
        tools.append({
            "name": command_to_tool_name(brief["id"]),
            "description": f"{desc} [command={brief['id']}]",
            "inputSchema": params,
        })
    return tools


def _meta_tool_defs() -> list[dict[str, Any]]:
    schemas = {
        "ze_entities": {"type": "object", "properties": {
            "type": {"type": "string"},
            "include_data": {"type": "boolean",
                             "description": "true 时追加实体完整 data(过项目 ACL,只对可访问项目)"}}},
        "ze_manual": {"type": "object", "properties": {
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1}}},
        "ze_oplog": {"type": "object", "properties": {
            "type": {"type": "string"}, "id": {"type": "string"},
            "source": {"type": "string"}, "limit": {"type": "integer"}}},
        "ze_read_asset": {"type": "object", "properties": {
            "sha256": {"type": "string", "minLength": 64, "maxLength": 64,
                       "description": "资产内容地址(64 位十六进制 sha256)"},
            "mode": {"type": "string", "enum": ["meta", "text", "describe"],
                     "description": "meta=元数据 / text=文本内容 / describe=直接看到该图片"},
        }, "required": ["sha256", "mode"]},
        "ze_file": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "相对关联 GitHub 仓库根的路径;空字符串=仓库根目录"},
        }},
        "ze_cancel": {"type": "object", "properties": {
            "token": {"type": "string"},
            "_idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200},
        }, "required": ["token"]},
        "ze_guide": {"type": "object", "properties": {
            "tour_id": {"type": "string",
                        "description": "ze_describe.guided_tours 里某条引导的 id"},
            "steps": {"type": "array",
                      "description": "自定义引导步(kind: say/point/highlight/act/wait/ask);"
                                     "act 步须带 command_id,可带 args",
                      "items": {"type": "object", "properties": {
                          "kind": {"type": "string",
                                   "enum": ["say", "point", "highlight", "act", "wait", "ask"]},
                          "say": {"type": "string"},
                          "target": {"type": "string"},
                          "command_id": {"type": "string"},
                          "args": {"type": "object"},
                      }, "required": ["kind"]}},
        }},
    }
    return [{"name": name, "description": desc,
             "inputSchema": schemas.get(name, {"type": "object", "properties": {}})}
            for name, desc in _META_TOOLS.items()]


def _tools_list(repo: Any, actor_ctx: McpActorCtx) -> list[dict[str, Any]]:
    """命令工具按目标 project 的 cmd_reg 列(命令是 per-project register)。"""
    tools = list(_meta_tool_defs())
    try:
        pid = _resolve_project_id(repo, actor_ctx.project_hint)
        ctx = _member_context(repo, pid, actor_ctx, need_edit=False)
        lifecycle_guard = getattr(ctx, "lifecycle_guard", nullcontext)
        with lifecycle_guard():
            with ctx.lock:
                tools = _command_tools(ctx.cmd_reg) + tools
    except ZeError:
        # 多项目未指定 / 无项目 / 无权:仍回元工具(agent 可先 ze_list_projects 再带 ?project=)。
        pass
    return tools


def _text_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _image_content_result(data: bytes, mime: str) -> dict[str, Any]:
    """MCP 原生 image content block:base64 + mimeType,MCP 客户端(Claude Code /
    Codex / Cursor)直接渲染成可见图片 —— 让 AI 真看到资产。"""
    return {"content": [{
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "mimeType": mime,
    }], "isError": False}


def _read_asset_result(ctx: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """ze_read_asset：过 durable ACL 闸门后按 mode 返回资产(须持 ctx.lock 调用)。

    三模式全部先过 durable_asset_access + asset_ref_for_read_locked —— 与
    HTTP /api/asset 同一道闸门:只放行本项目本地 blob 或被 canonical Document 引用的
    共享 blob;否则 acl_denied,绝不回 blob(无资产读旁路)。
    """
    sha256 = str(arguments.get("sha256", "") or "").strip().lower()
    mode = str(arguments.get("mode", "") or "").strip()
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ZeError("validation_failed", "sha256 须为 64 位十六进制")
    if mode not in ("meta", "text", "describe"):
        raise ZeError("validation_failed", "mode 须为 meta / text / describe 之一")

    _acl, _config, durable_refs = durable_asset_access(ctx)
    ref = asset_ref_for_read_locked(ctx, sha256, durable_refs)
    if ref is None:
        # 越权 / 非本项目引用 → 拒,不泄露 blob(也不区分「不存在」以免侧信道)。
        raise ZeError("acl_denied", "no access to this asset",
                      context={"sha256": sha256})

    if mode == "meta":
        return _text_result({"sha256": ref.sha256, "mime": ref.mime,
                             "size": ref.size, "ext": ref.ext})

    if mode == "text":
        if not _is_text_mime(ref.mime):
            return _text_result(
                {"error": {"code": "not_text_asset",
                           "message": f"asset mime {ref.mime!r} 非文本,text 模式不适用;"
                                      f"图片用 mode=describe,其余用 mode=meta"}},
                is_error=True)
        data = ctx.asset_store.get(ref)
        text = data.decode("utf-8", errors="replace")
        truncated = len(text) > _READ_ASSET_TEXT_MAX
        return _text_result({
            "sha256": ref.sha256, "mime": ref.mime, "size": ref.size,
            "truncated": truncated,
            "text": text[:_READ_ASSET_TEXT_MAX] if truncated else text,
        })

    # mode == "describe":MCP 原生 image content block 让 AI 真看到图片。
    if not str(ref.mime or "").lower().startswith("image/"):
        return _text_result(
            {"error": {"code": "not_viewable_asset",
                       "message": (f"asset mime {ref.mime!r} 无法经 MCP image content block "
                                   f"内联(仅 image/*);文本用 mode=text,其余用 mode=meta")}},
            is_error=True)
    if ref.size > _READ_ASSET_IMAGE_MAX_BYTES:
        return _text_result(
            {"error": {"code": "asset_too_large",
                       "message": (f"image {ref.size} 字节超过内联上限 "
                                   f"{_READ_ASSET_IMAGE_MAX_BYTES};改用 mode=meta")}},
            is_error=True)
    data = ctx.asset_store.get(ref)
    return _image_content_result(data, ref.mime)


def _github_file_result(
    repo: Any, actor_ctx: McpActorCtx, project_id: str, arguments: dict[str, Any],
) -> dict[str, Any]:
    """ze_file:读**当前项目**关联的 GitHub 仓库里的一个相对路径(R-C17)。

    owner/repo/ref 权威 = 项目级 git 关联 sidecar(read_project_git_link);PAT = 账号级
    credential 按本 actor 实时解析(find_for_user 只返本人记录,天然无越权)。项目未关联
    时回退账号级 credential 的历史 owner/repo/ref —— 迁移期 fail-safe,回退整组取用、
    绝不逐字段与项目链接交叉拼接(残缺链接已在 read_link 层判无效)。修掉旧行为「项目
    A/B 的 AI 读同一账号级仓库」的串仓库语义。token 只经 reveal_secret 服务端取出,
    绝不回带。未绑 / 上游错误一律 isError 结构化返回,不静默。
    """
    path = str(arguments.get("path", "") or "")
    store = getattr(repo, "credential_store", None)
    cred = (
        store.find_for_user(actor_ctx.actor.user_id, providers=(PROVIDER_GITHUB_REPO,))
        if store is not None else None
    )
    # (1) owner/repo/ref ← 项目级链接(权威);读取异常按未关联,绝不拖垮 ze_file
    link = None
    try:
        link = repo.read_project_git_link(project_id)
    except Exception:   # noqa: BLE001
        link = None
    if link is not None:
        owner = str(link.get("owner") or "")
        repo_name = str(link.get("repo") or "")
        ref = str(link.get("ref") or "")
    elif cred is not None:
        # legacy fallback:项目未关联 → 账号级历史绑定(整组取用)
        owner = str(cred.metadata.get(META_REPO_OWNER, "") or "")
        repo_name = str(cred.metadata.get(META_REPO_NAME, "") or "")
        ref = str(cred.metadata.get(META_REPO_REF, "") or "")
    else:
        owner = repo_name = ref = ""
    if not owner or not repo_name:
        return _text_result(
            {"error": {"code": "github_not_bound",
                       "message": "当前项目未关联 GitHub 仓库;在「文件 → 项目关联…」里填 owner/repo"}},
            is_error=True)
    # (2) PAT ← 账号级(密封复用, 所有项目共用)
    if cred is None:
        return _text_result(
            {"error": {"code": "github_pat_not_bound",
                       "message": "项目已关联仓库,但当前账号未绑定 GitHub 访问令牌;在「项目关联…」的令牌段绑定"}},
            is_error=True)
    try:
        token = store.reveal_secret(actor_ctx.actor.user_id, cred.id)
        result = github_read_path(token, owner, repo_name, path, ref)
    except ZeError as exc:
        # token 绝不入错误体;只回 code + 人读 message(github 客户端已保证 context 无 token)。
        return _text_result(
            {"error": {"code": exc.code, "message": public_error_message(exc)}},
            is_error=True)
    return _text_result(result)


def _guide_dispatch_act(
    repo: Any, actor_ctx: McpActorCtx, ctx: Any, step: dict[str, Any],
) -> dict[str, Any]:
    """引导 act 步 → dispatch(source='ai'):复用命令工具同一条提交 + 确认闸门,不开旁路。

    高危命令(requires_confirm)返回 PendingConfirmation 而非执行;MCP 绝不自批
    (确认只存在于网页人工通道)。args 缺省 {}(逻辑层步不带具体参数)。
    """
    command_id = str(step.get("command_id", "") or "")
    args = dict(step.get("args") or {})
    if not ctx.cmd_reg.has(command_id):
        return {"command_id": command_id, "step_id": step.get("id", ""),
                "result": {"error": "unknown_command"}, "isError": True}
    lifecycle_guard = getattr(ctx, "lifecycle_guard", nullcontext)
    with lifecycle_guard():
        with ctx.lock:
            with credential_context(
                actor_ctx.actor,
                getattr(repo, "credential_store", None),
                getattr(repo, "policy_store", None),
                getattr(repo, "tool_id", ""),
            ):
                execution = ctx.commit.execute(lambda: ctx.cmd_reg.dispatch(
                    command_id, args, actor=actor_ctx.actor, source="ai",
                    doc=ctx.doc, op_log=None,
                ))
            payload = commit_execution_payload(execution, document=ctx.doc)
            pending = execution.pending_confirmation is not None
            if pending:
                payload["next"] = "该操作必须由用户在网页界面人工批准"
                # 确认拉起:通知网页(帧无 nonce;批准材料只走 cookie 专属端点)
                broker = getattr(ctx, "broker", None)
                if broker is not None:
                    broker.publish_change(pending_confirmation_frame(
                        execution.pending_confirmation, doc_id=ctx.doc.id,
                    ))
            return {"command_id": command_id, "step_id": step.get("id", ""),
                    "pending": pending, "result": payload}


def _run_guided_tour(
    repo: Any, actor_ctx: McpActorCtx, ctx: Any, project_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """ze_guide:解析 tour(tour_id 投影 / steps 自定义)→ 逐步流式 guide_step 帧;
    act 步过确认闸门 dispatch;收尾 guide_end 帧。复用现有广播通道(ctx.broker)。"""
    import uuid

    tour_id = str(arguments.get("tour_id", "") or "").strip()
    raw_steps = arguments.get("steps")
    lifecycle_guard = getattr(ctx, "lifecycle_guard", nullcontext)
    with lifecycle_guard():
        with ctx.lock:
            commands = list_commands(ctx.cmd_reg)
    confirm_lookup = {c["id"]: bool(c.get("requires_confirm")) for c in commands}

    if tour_id:
        tours = compile_guided_tours(
            tool_understanding=getattr(repo.introspect, "tool_understanding", None),
            ai_flow=repo.introspect.ai_flow, manual=repo.introspect.manual,
            commands=commands,
        )
        tour = find_tour(tours, tour_id)
        if tour is None:
            raise ZeError("validation_failed", f"no guided tour {tour_id!r}",
                          context={"available": [t["id"] for t in tours]})
        steps = tour["steps"]
        mode = tour["mode"]
    elif isinstance(raw_steps, list) and raw_steps:
        try:
            steps = normalize_adhoc_steps(raw_steps, confirm_lookup=confirm_lookup)
        except ValueError as exc:
            raise ZeError("validation_failed", str(exc)) from exc
        mode = adhoc_tour_mode(steps)
    else:
        raise ZeError("validation_failed", "ze_guide 需要 tour_id 或 steps 之一")

    run_id = uuid.uuid4().hex
    total = len(steps)
    doc_id = ctx.doc.id
    broker = getattr(ctx, "broker", None)
    acts: list[dict[str, Any]] = []
    status = "completed"
    for index, step in enumerate(steps):
        if broker is not None:
            broker.publish_change(guide_step_frame(run_id, index, total, step, doc_id))
        if step["kind"] == "act" and step.get("command_id"):
            acts.append(_guide_dispatch_act(repo, actor_ctx, ctx, step))
    if broker is not None:
        broker.publish_change(guide_end_frame(run_id, status, doc_id))
    return _text_result({
        "runId": run_id, "tour_id": tour_id or None, "mode": mode,
        "total": total, "status": status, "acts": acts,
    })


def _run_idempotent_write(
    repo: Any,
    actor_ctx: McpActorCtx,
    *,
    project_id: str,
    tool_name: str,
    key: str,
    arguments: dict[str, Any],
    action: Any,
) -> dict[str, Any]:
    """Give MCP writes the same business idempotency seam as HTTP and CLI."""
    if not key:
        return action()
    store = getattr(repo, "idempotency_store", None)
    if store is None:
        raise ZeError(
            "idempotency_store_unavailable",
            "MCP write supplied an idempotency key but no ledger is configured",
            context={"tool": tool_name, "project_id": project_id},
        )
    actor_id = getattr(actor_ctx.actor, "user_id", "")
    return store.run(
        scope=f"mcp.{tool_name}:project={project_id}:actor={actor_id}",
        key=key,
        request_payload={
            "tool": tool_name,
            "project_id": project_id,
            "actor": actor_id,
            "arguments": arguments,
        },
        action=action,
        outcome_unknown_classifier=commit_outcome_unknown,
    )


def _call_tool(repo: Any, actor_ctx: McpActorCtx, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """tools/call 分派。命令执行类错误以 isError 在带内返回(MCP 约定);
    鉴权/协议类错误由上层转 JSON-RPC error。"""
    arguments = dict(arguments or {})
    arg_project = str(arguments.pop("_project", "") or "")
    idempotency_key = str(arguments.pop("_idempotency_key", "") or "").strip()
    project_hint = arg_project or actor_ctx.project_hint

    # ---- skill 级元工具(无需 project)----
    if name == "ze_list_projects":
        # token 模式:先过工具级 ban,再**按项目 ACL 过滤**(只列调用者可访问的,不泄露他人项目/owner)。
        # 本机全权模式:列全部(等同 CLI __system__)。
        if actor_ctx.enforce_acl:
            _require_tool_access(repo, actor_ctx.actor, need_edit=False)
        rows = []
        for pid in repo.list_project_ids():
            meta = repo.project_meta(pid)
            acl = meta.get("acl", {}) or {}
            if actor_ctx.enforce_acl and not _can_access(acl, actor_ctx.actor.user_id):
                continue
            rows.append({"id": pid, "name": meta.get("name"), "owner": acl.get("owner", "")})
        return _text_result({"projects": rows})

    # ---- project 级:解析项目 + Context ----
    need_edit = name not in _READ_META_TOOLS
    pid = _resolve_project_id(repo, project_hint)
    ctx = _member_context(repo, pid, actor_ctx, need_edit=need_edit)
    lifecycle_guard = getattr(ctx, "lifecycle_guard", nullcontext)

    # ze_file:项目 ACL 已在 _member_context 里过;GitHub 是外部只读源,读取走网络,
    # 故不进 ctx.lock(避免网络往返期间锁住整个项目)。
    if name == "ze_file":
        return _github_file_result(repo, actor_ctx, pid, arguments)

    if name in _READ_META_TOOLS:
        with lifecycle_guard():
            with ctx.lock:
                if name == "ze_describe":
                    return _text_result(describe_context(
                        ctx, introspect=repo.introspect, config=repo.config,
                    ))
                if name == "ze_entities":
                    return _text_result(list_entities(
                        ctx.doc, arguments.get("type"),
                        include_data=bool(arguments.get("include_data")),
                    ))
                if name == "ze_manual":
                    return _text_result(read_manual(
                        repo.introspect,
                        offset=arguments.get("offset") or 0,
                        limit=arguments.get("limit"),
                    ))
                if name == "ze_read_asset":
                    return _read_asset_result(ctx, arguments)
                return _text_result(query_oplog(
                    ctx.op_log, entity_type=arguments.get("type"), entity_id=arguments.get("id"),
                    source=arguments.get("source"), limit=arguments.get("limit")))

    if name == "ze_cancel":
        token = str(arguments.get("token", ""))
        def _cancel_pending():
            with lifecycle_guard():
                with ctx.lock:
                    return _text_result({
                        "cancelled": ctx.cmd_reg.pending_store.cancel(
                            token, actor=actor_ctx.actor,
                        ),
                    })

        return _run_idempotent_write(
            repo, actor_ctx, project_id=pid, tool_name=name,
            key=idempotency_key, arguments=arguments, action=_cancel_pending,
        )

    if name == "ze_guide":
        # 引导驱动:每个 act 步各自进 dispatch + 确认闸门(见 _guide_dispatch_act);
        # 不整体套 request-ledger(idempotency 是 per-write 语义)。
        return _run_guided_tour(repo, actor_ctx, ctx, pid, arguments)

    # ---- 命令工具 → dispatch(source='ai')----
    command_id = resolve_command_id(ctx.cmd_reg, name)
    if command_id is None:
        return _text_result({"error": "unknown_tool", "tool": name}, is_error=True)
    # §7.3 W1-4 ④「调用工具」段:带上具体工具名,过程卡才说得出「正在调用 批量生成」,
    # 而不是干巴巴一句「处理中」。轮内的其余四段在 server 的 chat 管线打点。
    try:
        from sdk.assistant_turn import STAGE_TOOL, stage_frame
        _broker = getattr(ctx, "broker", None)
        if _broker is not None:
            _broker.publish_change(stage_frame("", STAGE_TOOL, doc_id=ctx.doc.id, tool=command_id))
    except Exception:   # noqa: BLE001 — 呈现事件,坏了不影响命令执行
        pass
    def _dispatch_command():
        with lifecycle_guard():
            with ctx.lock:
                with credential_context(
                    actor_ctx.actor,
                    getattr(repo, "credential_store", None),
                    getattr(repo, "policy_store", None),
                    getattr(repo, "tool_id", ""),
                ):
                    execution = ctx.commit.execute(lambda: ctx.cmd_reg.dispatch(
                        command_id, arguments, actor=actor_ctx.actor, source="ai",
                        doc=ctx.doc, op_log=None,
                        # §7.4 P0-E:命中确认闸门时,产生的 pending 会带上这一轮的 id,
                        # 取消整轮才知道该释放哪些 nonce(不传 = 释放永远是空操作)。
                        turn_id=actor_ctx.turn_id,
                    ))
                payload = commit_execution_payload(execution, document=ctx.doc)
                if execution.pending_confirmation is not None:
                    payload["next"] = "该操作必须由用户在网页界面人工批准"
                    # 确认拉起:通知网页(帧无 nonce;批准材料只走 cookie 专属端点)
                    broker = getattr(ctx, "broker", None)
                    if broker is not None:
                        broker.publish_change(pending_confirmation_frame(
                            execution.pending_confirmation, doc_id=ctx.doc.id,
                        ))
                return _text_result(payload)

    return _run_idempotent_write(
        repo, actor_ctx, project_id=pid, tool_name=name,
        key=idempotency_key, arguments=arguments, action=_dispatch_command,
    )


# ==============================================================
# JSON-RPC 协议核心(纯函数,离线可测)
# ==============================================================

def _rpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def handle_mcp_message(repo: Any, message: dict[str, Any], actor_ctx: McpActorCtx) -> dict[str, Any] | None:
    """单条 JSON-RPC 消息 → 响应 dict(通知返回 None)。纯函数:不碰 HTTP。"""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _rpc_error(message.get("id") if isinstance(message, dict) else None,
                          _INVALID_REQUEST, "invalid JSON-RPC 2.0 request")
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}
    is_notification = "id" not in message

    # 通知(initialized 等):无响应。
    if is_notification:
        return None

    try:
        if method == "initialize":
            client_proto = (params or {}).get("protocolVersion")
            proto = client_proto if client_proto in _SUPPORTED_PROTOCOLS else MCP_DEFAULT_PROTOCOL
            return _rpc_result(req_id, {
                "protocolVersion": proto,
                "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
                "instructions": ("ZE 工具的 MCP 接口。tools 即本工具全部 AI 可调命令 + ze_* 元工具"
                                 "(ze_describe 看对象图、ze_entities[include_data]/ze_oplog 读、"
                                 "ze_manual 读说明书；高风险操作须转网页人工批准)。"
                                 "多项目时在 MCP URL 加 ?project=<id> 或工具 arg 传 _project。"),
            })
        if method == "ping":
            return _rpc_result(req_id, {})
        if method == "tools/list":
            return _rpc_result(req_id, {"tools": _tools_list(repo, actor_ctx)})
        if method == "tools/call":
            name = (params or {}).get("name", "")
            arguments = (params or {}).get("arguments", {}) or {}
            try:
                return _rpc_result(req_id, _call_tool(repo, actor_ctx, name, arguments))
            except ZeError as e:
                # 鉴权/项目类错误转 JSON-RPC error;命令执行错误已在 _call_tool 内带内 isError 处理。
                if e.code in ("auth_required", "acl_denied", "read_only_violation"):
                    raise
                return _rpc_result(req_id, _text_result(
                    {"error": {"code": e.code, "message": public_error_message(e)}}, is_error=True))
            except Exception as error:
                if not is_commit_authority_error(error):
                    raise
                return _rpc_result(req_id, _text_result(
                    {"error": _commit_authority_error_body(error)},
                    is_error=True,
                ))
        if method == "resources/list":
            return _rpc_result(req_id, {"resources": [
                {"uri": "ze://context", "name": "Context.Describe", "mimeType": "application/json"},
                {"uri": "ze://entities", "name": "Entity.List", "mimeType": "application/json"},
                {"uri": "ze://oplog", "name": "OperationLog.Query", "mimeType": "application/json"},
            ]})
        if method == "resources/read":
            uri = (params or {}).get("uri", "")
            pid = _resolve_project_id(repo, actor_ctx.project_hint)
            ctx = _member_context(repo, pid, actor_ctx, need_edit=False)
            lifecycle_guard = getattr(ctx, "lifecycle_guard", nullcontext)
            with lifecycle_guard():
                with ctx.lock:
                    if uri == "ze://context":
                        body = describe_context(ctx, introspect=repo.introspect, config=repo.config)
                    elif uri == "ze://entities":
                        body = list_entities(ctx.doc)
                    elif uri == "ze://oplog":
                        body = query_oplog(ctx.op_log, limit=50)
                    else:
                        return _rpc_error(req_id, _INVALID_PARAMS, f"unknown resource {uri!r}")
            return _rpc_result(req_id, {"contents": [
                {"uri": uri, "mimeType": "application/json",
                 "text": json.dumps(body, ensure_ascii=False, indent=2)}]})
        return _rpc_error(req_id, _METHOD_NOT_FOUND, f"unknown method {method!r}")
    except ZeError as e:
        return _rpc_error(
            req_id, _rpc_code_for(e.code), public_error_message(e), data={"code": e.code},
        )
    except Exception as e:  # 兜底:不让单条消息崩整个端点
        if is_commit_authority_error(e):
            return _rpc_error(
                req_id,
                _rpc_code_for(e.code),
                public_error_message(e),
                data={
                    "code": e.code,
                    **public_commit_authority_context(e),
                },
            )
        return _rpc_error(req_id, _INTERNAL_ERROR, "internal error")


# ==============================================================
# Surface 判定 + FastAPI 挂载
# ==============================================================

def mcp_surface_enabled(root: Path = ROOT) -> bool:
    """声明了 mcp surface(读 surface 档案 ZE_SURFACE.json 的 surfaces)才挂;显式声明、不自动推断。"""
    # 文件名拼接写法:规避 ops-contract 的 `ZE_<NAME>` 字面量正则把这个**文件名**误判为 env。
    surface_file = "ZE_" "SURFACE.json"
    try:
        prof = json.loads((root / surface_file).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return isinstance(prof, dict) and "mcp" in (prof.get("surfaces") or [])


def register_mcp_routes(app: Any, repo: Any) -> None:
    """在 server.py 的 catch-all mount 之前调:挂 /mcp(POST=JSON-RPC;GET=405)。

    用 Starlette 原始路由 `app.add_route`(端点按位置收 Request、返回 Response),不走
    FastAPI 的注解依赖注入 —— 本模块有 `from __future__ import annotations`,注解全成字符串,
    走 add_api_route 会把 `request` 误判为 query 参数。fastapi 仍懒加载(CLI import sdk 不需它)。
    """
    from starlette.responses import JSONResponse, Response   # lazy(fastapi 自带 starlette)

    async def mcp_endpoint(request):
        if request.method == "GET":
            return JSONResponse(
                _rpc_error(None, _METHOD_NOT_FOUND, "GET not supported (stateless tools-only MCP)"),
                status_code=405)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(_rpc_error(None, _PARSE_ERROR, "parse error"), status_code=400)
        try:
            actor_ctx = _resolve_http_actor_context(request, repo)
        except ZeError as e:
            return JSONResponse(_rpc_error(
                None, _rpc_code_for(e.code), public_error_message(e), data={"code": e.code},
            ),
                                status_code=_http_status_for(e.code))

        # 批量(数组)与单条都支持;通知(无 id)无响应。
        messages = body if isinstance(body, list) else [body]
        responses: list[dict[str, Any]] = []
        for msg in messages:
            try:
                resp = handle_mcp_message(repo, msg, actor_ctx)
            except ZeError as e:  # 鉴权类 → JSON-RPC error
                resp = _rpc_error(
                    msg.get("id") if isinstance(msg, dict) else None,
                    _rpc_code_for(e.code), public_error_message(e), data={"code": e.code},
                )
            if resp is not None:
                responses.append(resp)
        if not responses:
            return Response(status_code=202)            # 全是通知
        out = responses if isinstance(body, list) else responses[0]
        # 单条响应若是鉴权错,设 401/403 让非 JSON-RPC 感知的客户端也能识别。
        status = 200
        if isinstance(out, dict) and isinstance(out.get("error"), dict):
            status = _http_status_for((out["error"].get("data") or {}).get("code", ""))
        return JSONResponse(out, status_code=status)

    app.add_route("/mcp", mcp_endpoint, methods=["POST", "GET"])


__all__ = [
    "handle_mcp_message", "McpActorCtx",
    "register_mcp_routes", "mcp_surface_enabled",
    "command_to_tool_name", "resolve_command_id", "require_token_mode", "allow_loopback_trust",
    "MCP_DEFAULT_PROTOCOL", "MCP_LOCAL_USER_ID",
]
