"""server.py — ZE-skill-build 多项目 + 账号后端 (FastAPI 进程内单例).

启动 `bootstrap()` 一次 → `Repository`(全局 UserStore + SessionManager + 按 project_id
懒加载的 per-project Context 缓存)。

鉴权模型(轻量真账号,内网):
  - 身份:用户名+密码注册/登录 → session token(Bearer / cookie；通用旧接口仍兼容
    ?token=，但框架只为 SSE 生成；asset 端点明确忽略主 session query token);
    `resolve_actor` 解析每个请求的 actor,匿名 = Actor(user_id="")。
  - 隔离:每 project 的 acl={owner,collaborators}。`_require_member` 校当前 actor 是
    owner / collaborator(owner 空 = 无主公共,登录用户皆可);否则 401(未登录)/403(无权)。
  - 协作:`document.set_acl`(owner-only,server 层校 owner)经 Operation 广播给在线协作者。
  - CLI / AI 管线用保留账号 __system__(走 bootstrap,不经本 HTTP ACL)。

数据变更:POST /api/command/{id} → 校权 → dispatch → (save_policy 自动提交) save → 回 changes;
GET /api/events 用 SSE 把该 project 的 Broker entity_change 实时推给前端。
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from sdk import (
    Actor, ZeError, resolve_actor,
    to_status_payload, to_pipeline_status_payload,
    describe_context, list_commands, describe_command, list_entities, entity_select,
    list_pipelines, compile_guided_tours,
    diagnose_document, diagnose_build_report,
    list_editors, list_tasks, task_status, query_oplog, read_manual,
    compose_copilot_context, structural_entity_summary, fold_data_channel,
    AssetStore, MutationBatch,
    PERMISSION_EDIT, PERMISSION_READ, PERMISSION_BAN, PERMISSION_NONE,
    permission_allows, USER_STATUS_DISABLED, is_super_admin,
    MODEL_SCOPES,
    user_public_card, search_users, lookup_users,
    credential_context, PROVIDERS, CLI_PROVIDERS,
    PROVIDER_HUPO_GATEWAY, PROVIDER_OPENAI_API, PROVIDER_ANTHROPIC_API,
    PROVIDER_CURSOR_API,
    PROVIDER_CODEX_CLI, PROVIDER_CLAUDE_CLI, PROVIDER_GITHUB_REPO,
    PROVIDER_GEMINI_API,
    META_HOME_DIR, META_DEFAULT,
    META_REPO_OWNER, META_REPO_NAME, META_REPO_REF,
    github_validate_owner, github_validate_repo, github_validate_ref,
    provision_cli_home, write_cli_auth_file,
    cli_home_env, agent_chat, cursor_models_for_current_user, LOGIN_MANAGER,
)
from sdk.bootstrap import Context, bootstrap
from sdk.asset_access import (
    asset_ref_for_read_locked as _asset_ref_for_read_locked,
    durable_asset_access as _durable_asset_access,
    scan_durable_asset_refs as _scan_durable_asset_refs,
)
from sdk.persistence import commit_execution_payload, execute_confirmation
from sdk._operation_projection import public_oplog_entries
from sdk.error_projection import (
    commit_outcome_unknown,
    is_commit_authority_error,
    public_error_message,
)
from sdk.telemetry import UsageEmitter, anon_user, read_tool_id
from document import Document, read_document_metadata, validate_document_acl
from capacity import CapacityChecker, CapacityLimits
from history_authority import HistoryAuthorityError
from pipeline_authority_commit import PipelineAuthorityCommitError
from auth import auth_runtime_config, normalize_local_login_name

# ---- 进程内常驻:多项目仓库 ----
_repo = bootstrap()

# ---- 旁路使用埋点(push 到中心采集器;未配 ZE_USAGE_COLLECTOR_URL 则全程 no-op)----
# fire-and-forget:emit 只入队不阻塞,异常全吞,绝不影响请求主流程。
_usage = UsageEmitter(
    tool_id=read_tool_id(Path(__file__).resolve().parent),
    collector_url=os.environ.get("ZE_USAGE_COLLECTOR_URL"),
)


def _command_label(ctx: Context, command_id: str) -> Optional[str]:
    """best-effort 取命令 label(i18n key / 文本)作 feature_label;取不到回 None。"""
    try:
        declaration = getattr(ctx.cmd_reg, "registered_declaration", None)
        if callable(declaration):
            return declaration(command_id).get("label", "") or None
        return getattr(ctx.cmd_reg.get(command_id), "label", "") or None
    except Exception:
        return None


@asynccontextmanager
async def _server_lifespan(_app: FastAPI):
    try:
        yield
    finally:
        LOGIN_MANAGER.shutdown_all()


app = FastAPI(title="ZE-skill-build", version="0.2.0", lifespan=_server_lifespan)
app.add_middleware(
    CORSMiddleware,
    # dev: 允许 localhost 任意端口 (vite 5173 被占时会自动换口)
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,            # 支持同源 cookie 会话(dev 跨端口用 Bearer/query)
    allow_methods=["GET", "POST", "PUT", "DELETE"],   # PUT: /api/config;DELETE: /api/projects/{id}(header-v2)
    allow_headers=["*"],
)

LARK_AUTH_BASE_URL = os.environ.get(
    "ZE_LARK_AUTH_BASE_URL",
    "http://larkauth.tools.hupogames.com",
).rstrip("/")
SESSION_COOKIE_NAME = "ze_session"
SESSION_COOKIE_MAX_AGE = 30 * 86400


@app.middleware("http")
async def _no_cache_spa_shell(request, call_next):
    """SPA 外壳(index.html)发 no-cache,强制每次回源校验。

    StaticFiles 默认只给 etag/last-modified、无 Cache-Control → 浏览器启发式缓存,
    部署/重建后用户一直拿旧 index.html(引用旧 JS),界面不更新(stale UI)。
    内容寻址的 /assets/*(vite 哈希命名)可继续缓存,不动。
    """
    resp = await call_next(request)
    if request.url.path.startswith("/api/asset/"):
        # Success and error responses alike are authority-dependent. A shared
        # proxy/browser cache must never replay bytes (or an authorization
        # decision) to another principal after ACL/generation changes.
        resp.headers["Cache-Control"] = "private, no-store, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Vary"] = "Authorization, Cookie"
        resp.headers["Referrer-Policy"] = "no-referrer"
    elif resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif request.url.path.startswith("/assets/"):
        # vite 哈希命名的 /assets/*(内容寻址,改一字节即换文件名)→ 永久不可变缓存。配合 index.html
        # no-cache:每次部署出新 index.html(指向新哈希),浏览器拿新 index → 取新 assets URL,旧 assets
        # 缓存留着不碍事。StaticFiles 默认不发 Cache-Control → 浏览器启发式缓存,这里补显式 immutable。
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp

# ZeError.code → HTTP status。401=未登录(前端跳登录),403=已登录但无权。
_STATUS_BY_CODE = {
    "version_conflict": 409,
    "cas_version_mismatch": 409,
    "user_exists": 409,
    "entity_not_found": 404,
    "unknown_command": 404,
    "entity_file_missing": 404,
    "project_not_found": 404,
    "not_found": 404,
    "asset_missing": 404,
    "schema_violation": 422,
    "validation_failed": 422,
    "idempotency_key_invalid": 422,
    "idempotency_key_required": 422,
    "idempotency_key_reused": 409,
    "idempotency_in_progress": 409,
    "idempotency_outcome_unknown": 409,
    "idempotency_store_corrupt": 500,
    "producer_effect_forbidden": 500,
    "mutation_rollback_failed": 500,
    "pipeline_replay_unsafe": 409,
    "pipeline_authority_commit_invalid": 409,
    "pipeline_authority_root_invalid": 409,
    "pipeline_authority_seal_required": 409,
    "pipeline_authority_seal_invalid": 409,
    "pipeline_start_missing": 409,
    "pipeline_start_ambiguous": 409,
    "pipeline_start_prefix_incomplete": 409,
    "pipeline_start_identity_mismatch": 409,
    "pipeline_start_digest_mismatch": 409,
    "pipeline_control_invalid": 409,
    "pipeline_authority_terminal": 409,
    "pipeline_authority_stale": 409,
    "pipeline_authority_unavailable": 503,
    "pipeline_reconcile_required": 409,
    "asset_reachability_required": 422,
    "asset_upload_too_large": 413,
    "media_ticket_invalid": 403,
    "capacity_exceeded": 413,
    "manifest_path_forbidden": 403,
    "config_version_conflict": 409,
    "config_write_failed": 500,
    "config_durability_unknown": 500,
    "config_load_invalid": 500,
    "confirm_token_unknown": 404,
    "confirmation_actor_mismatch": 403,
    "confirmation_in_progress": 409,
    "confirmation_repair_required": 409,
    "confirmation_reservation_stale": 409,
    "confirmation_human_required": 403,
    "confirmation_capacity_exceeded": 429,
    "credential_store_corrupt": 500,
    "credential_store_unavailable": 500,
    "credential_store_permissions_invalid": 500,
    "credential_store_write_failed": 500,
    "credential_store_durability_unknown": 500,
    "credential_key_unavailable": 500,
    "credential_key_invalid": 500,
    "credential_key_permissions_invalid": 500,
    "credential_key_write_failed": 500,
    "credential_key_durability_unknown": 500,
    "credential_cli_auth_write_failed": 500,
    "credential_cli_auth_durability_unknown": 500,
    "credential_provider_immutable": 409,
    # github_repo 只读客户端(系统 1b):诚实上游错误分类。
    "github_not_bound": 428,
    "github_unauthorized": 502,
    "github_forbidden": 502,
    "github_not_found": 404,
    "github_rate_limited": 429,
    "github_unavailable": 502,
    "github_file_too_large": 413,
    "github_binary_file": 415,
    "github_unsupported_type": 415,
    "github_error": 502,
    "pipeline_completion_repair_required": 409,
    "commit_repair_required": 409,
    "project_topology_conflict": 409,
    "project_busy": 409,
    "project_generation_stale": 409,
    "project_recovery_required": 409,
    "history_authority_mismatch": 409,
    "project_topology_write_failed": 500,
    "project_topology_durability_unknown": 500,
    "project_topology_unavailable": 503,
    "document_storage_unsupported": 503,
    "document_lock_unavailable": 503,
    "document_storage_path_unsafe": 500,
    "document_storage_layout_invalid": 409,
    "document_storage_mapping_collision": 500,
    "entity_file_identity_mismatch": 409,
    "document_write_failed": 500,
    "document_durability_unknown": 500,
    "guest_session_required": 409,
    "acl_denied": 403,
    "read_only_violation": 403,
    "model_scope_denied": 403,
    "auth_required": 401,
    "auth_failed": 401,
    "auth_method_disabled": 403,
    "registration_closed": 403,
    "auth_configuration_invalid": 500,
    "requires_credential": 428,
    "credential_unavailable": 428,
}


def _error_response(e: ZeError, *, topology_request: bool = False) -> JSONResponse:
    status = _STATUS_BY_CODE.get(e.code, 400)
    # message 用纯人读文案(getattr 兼容旧 ZeError);空 message 退回裸 code,不带 [code] 前缀
    msg = public_error_message(e)
    error: dict[str, Any] = {"code": e.code, "message": msg}
    # 只暴露客户端决策所需、不会泄漏本机路径/密钥的修复事实。
    context = dict(getattr(e, "context", {}) or {})
    if (
        context.get("outcome_unknown") is True
        and context.get("durability_phase") in {
            "commit_outcome",
            "post_write_verification",
        }
    ):
        # A durable/possibly-durable write must not look like a transient 5xx
        # to HTTP middleware.  409 means the same intent is fenced until the
        # client reconciles or an operator repairs it.  Preflight unavailable
        # errors keep their ordinary (often 503) classification.
        status = 409
    if topology_request:
        context.setdefault("mutation_outcome", "unknown")
        error["mutation_outcome"] = context["mutation_outcome"]
    safe_context = {
        key: context[key]
        for key in (
            "stage", "replaced", "repair_required", "cause_code",
            "operation_id", "project_id", "expected_generation",
            "current_generation", "intent_id", "kind", "target_id",
            "mutation_outcome", "proof_status", "proof_generation",
            "reason", "outcome_unknown", "durability_phase", "commit_facts",
        )
        if key in context
    }
    if safe_context:
        error["context"] = safe_context
    if status == 428:
        # 「没绑就先说」不变量(3.56):428 必带机器可读 reason + 一键修复指引。
        provider = str(context.get("provider") or "")
        error.setdefault("reason", "credential_stale" if e.code == "credential_unavailable"
                         else "not_bound")
        error.setdefault("fix", {"action": "bind", "provider": provider,
                                 "capability": str(context.get("capability") or "")})
    return JSONResponse(status_code=status, content={"error": error})


def _mount_mod_server_routes() -> None:
    """Optionally let a skill-owned mod file mount extra FastAPI routes.

    Business-specific service routes belong in `02-mod/05-code/server_routes.py`.
    The managed server only provides the hook and common dependencies, so
    downstream capabilities can survive direct-copy core syncs.
    """
    root = Path(__file__).resolve().parent
    routes_path = root / "02-mod" / "05-code" / "server_routes.py"
    if not routes_path.exists():
        return
    mod_dir = str(routes_path.parent)
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)
    spec = importlib.util.spec_from_file_location("_ze_mod_server_routes", routes_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法载入 mod server routes: {routes_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    register = getattr(module, "register", None)
    if not callable(register):
        return
    register(
        app=app,
        repo=_repo,
        deps={
            "get_actor": _get_actor,
            "require_tool_access": _require_tool_access,
            "require_edit": _require_edit,
            "error_response": _error_response,
            "ZeError": ZeError,
        },
    )


def _strip_body_idempotency_key(body: Any) -> tuple[Any, str]:
    """Return body without private _idempotency_key plus body-supplied key."""
    if not isinstance(body, dict):
        return body, ""
    key = body.get("_idempotency_key")
    if "_idempotency_key" not in body:
        return body, ""
    clean = dict(body)
    clean.pop("_idempotency_key", None)
    return clean, str(key or "")


def _request_idempotency_key(request: Request, body_key: str = "") -> str:
    return (
        request.headers.get("Idempotency-Key")
        or request.headers.get("X-Idempotency-Key")
        or body_key
        or ""
    ).strip()


def _require_topology_request(
    request: Request,
    body: dict[str, Any],
    *,
    body_key: str = "",
) -> tuple[str, str]:
    """Validate the two identities that make a namespace write replayable.

    ``intent_id`` identifies the user's business action and is embedded in the
    topology marker/journal.  ``Idempotency-Key`` identifies the HTTP attempt
    ledger.  They deliberately remain separate and both are mandatory.
    """
    operation_id = body.get("intent_id")
    if (
        not isinstance(operation_id, str)
        or len(operation_id) != 32
        or any(char not in "0123456789abcdef" for char in operation_id)
    ):
        raise ZeError(
            "validation_failed",
            "intent_id must be 32 lowercase hex characters",
        )
    key = _request_idempotency_key(request, body_key)
    if not key:
        raise ZeError(
            "idempotency_key_required",
            "project lifecycle writes require Idempotency-Key",
        )
    return operation_id, key


def _run_idempotent(
    request: Request,
    *,
    actor: Actor,
    scope: str,
    project_id: str = "",
    body: Any = None,
    body_key: str = "",
    action: Callable[[], dict[str, Any]],
    outcome_unknown_codes: tuple[str, ...] = (),
    outcome_unknown_classifier: Callable[[BaseException], bool] | None = None,
) -> dict[str, Any]:
    key = _request_idempotency_key(request, body_key)
    request_payload = {
        "method": request.method,
        "path": request.url.path,
        "scope": scope,
        "project_id": project_id,
        "actor": actor.user_id,
        "body": body,
    }
    ledger_scope = f"{scope}:project={project_id}:actor={actor.user_id}"
    return _repo.idempotency_store.run(
        scope=ledger_scope,
        key=key,
        request_payload=request_payload,
        action=action,
        outcome_unknown_codes=outcome_unknown_codes,
        outcome_unknown_classifier=outcome_unknown_classifier,
    )


def _topology_http_error(
    exc: ZeError,
    *,
    intent_id: str,
    kind: str,
    target_id: str,
    principal_id: str,
) -> ZeError:
    """Classify an error only from durable intent evidence, never its code."""
    context = dict(getattr(exc, "context", {}) or {})
    mutation_outcome = "unknown"
    try:
        proof = _repo.project_intent_status(
            target_id, intent_id, kind=kind, principal_id=principal_id,
        )
    except Exception:
        proof = None
    if proof is not None:
        proof_status = str(proof.get("status") or "")
        if proof_status in ("committed", "deleted"):
            mutation_outcome = "committed_proof_via_reconcile"
        elif proof_status == "not_committed":
            mutation_outcome = "not_started"
        context["proof_status"] = proof_status
        if isinstance(proof.get("generation"), int):
            context["proof_generation"] = proof["generation"]
    context.update({
        "intent_id": intent_id,
        "kind": kind,
        "target_id": target_id,
        "mutation_outcome": mutation_outcome,
    })
    return ZeError(exc.code, str(getattr(exc, "message", "") or exc), context=context)


def _run_topology_idempotent(
    request: Request,
    *,
    actor: Actor,
    scope: str,
    project_id: str,
    body: Any,
    body_key: str,
    action: Callable[[], dict[str, Any]],
    intent_id: str,
    kind: str,
    target_id: str,
) -> dict[str, Any]:
    try:
        return _run_idempotent(
            request,
            actor=actor,
            scope=scope,
            project_id=project_id,
            body=body,
            body_key=body_key,
            action=action,
            outcome_unknown_codes=(
                "project_topology_durability_unknown",
                "project_recovery_required",
            ),
        )
    except ZeError as exc:
        raise _topology_http_error(
            exc, intent_id=intent_id, kind=kind, target_id=target_id,
            principal_id=actor.user_id,
        ) from exc


def _project_history_authority_error(exc: HistoryAuthorityError) -> ZeError:
    """Translate the neutral protocol error at the HTTP trust boundary."""
    if exc.outcome_unknown:
        message = (
            "canonical history outcome is unknown; do not retry this action; "
            "reconcile or repair the project"
        )
    else:
        message = "canonical history changed; reload the project before retrying"
    return ZeError(
        "history_authority_mismatch",
        message,
        context=dict(getattr(exc, "context", {}) or {}),
    )


def _history_outcome_unknown(exc: BaseException) -> bool:
    """Backward-compatible alias for the complete typed commit classifier."""
    return commit_outcome_unknown(exc)


def _commit_outcome_unknown(exc: BaseException) -> bool:
    """Classify typed post-write authority failures without matching text."""
    return commit_outcome_unknown(exc)


@app.exception_handler(HistoryAuthorityError)
async def _history_authority_error_handler(
    request: Request,
    exc: HistoryAuthorityError,
):
    del request
    return _error_response(_project_history_authority_error(exc))


def _project_pipeline_authority_error(exc: PipelineAuthorityCommitError) -> ZeError:
    """Project Pipeline preflight and post-durable failures without losing facts."""
    return ZeError(
        exc.code,
        public_error_message(exc),
        context=dict(getattr(exc, "context", {}) or {}),
    )


@app.exception_handler(PipelineAuthorityCommitError)
async def _pipeline_authority_error_handler(
    request: Request,
    exc: PipelineAuthorityCommitError,
):
    del request
    return _error_response(_project_pipeline_authority_error(exc))


@app.exception_handler(ZeError)
async def _ze_error_handler(request: Request, exc: ZeError):
    """Depends 校权 + route dispatch 抛的 ZeError 统一映射成 HTTP。"""
    path = request.url.path
    topology_request = (
        (request.method == "POST" and path == "/api/projects")
        or (request.method == "POST" and path.startswith("/api/projects/") and path.endswith("/duplicate"))
        or (request.method == "DELETE" and path.startswith("/api/projects/"))
        or (request.method == "POST" and path.startswith("/api/demo-templates/") and path.endswith("/open"))
    )
    response = _error_response(exc, topology_request=topology_request)
    if request.method == "GET" and (
        path == "/api/projects"
        or path == "/api/document"
        or "/intents/" in path
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


# ==============================================================
# 身份 / 鉴权 Depends
# ==============================================================

def _request_token(request: Request, query_token: Optional[str]) -> str:
    """从共享 cookie / Authorization: Bearer / ?token= 取 session token。"""
    auth = request.headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    return query_token or ""


def _get_actor(request: Request, token: Optional[str] = Query(default=None)) -> Actor:
    """解析请求身份;无 / 失效 token → 匿名 Actor(user_id="")。"""
    return resolve_actor(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get(SESSION_COOKIE_NAME),
        query_token=token,
        user_store=_repo.user_store,
        session_mgr=_repo.session_mgr,
    )


def _get_header_cookie_actor(request: Request) -> Actor:
    """Resolve ordinary credentials while deliberately ignoring URL tokens.

    Media URLs must contain only an asset-scoped ticket.  Keeping this separate
    from ``_get_actor`` prevents an old ``?token=<session>`` URL from remaining
    a supported asset authentication path.
    """
    return resolve_actor(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get(SESSION_COOKIE_NAME),
        query_token=None,
        user_store=_repo.user_store,
        session_mgr=_repo.session_mgr,
    )


def _get_transport_actor(
    request: Request, token: Optional[str] = Query(default=None),
) -> Actor:
    """Resolve native browser transports against the explicit local session.

    ``EventSource`` cannot attach an Authorization header, so the web client
    still carries its current local Bearer token in the query string.  Native
    media uses a short asset-scoped media ticket instead; it never reaches this
    dependency.  A shared SSO cookie may legitimately belong to another ZE
    tool/account, so SSE promotes its explicit query capability to Bearer
    precedence.  Ordinary REST keeps Bearer > cookie > query.
    """
    header = request.headers.get("authorization") or ""
    bearer = header if header[:7].lower() == "bearer " else ""
    explicit = bearer or (f"Bearer {token}" if token else None)
    return resolve_actor(
        authorization=explicit,
        cookie_token=request.cookies.get(SESSION_COOKIE_NAME),
        query_token=None,
        user_store=_repo.user_store,
        session_mgr=_repo.session_mgr,
    )


def _require_login(actor: Actor) -> None:
    if not actor.user_id:
        raise ZeError("auth_required", "login required")


def _effective_permission(user) -> str:
    if not user:
        return PERMISSION_BAN
    if user.status == USER_STATUS_DISABLED:
        return PERMISSION_BAN
    if is_super_admin(user.user_id):
        return PERMISSION_EDIT
    explicit = _repo.policy_store.user_permissions(user.user_id).get(_repo.tool_id)
    if explicit:
        return explicit
    # 工具默认档:管理台显式设过才夺权(标准 skill 设 none = 默认无权限、须申请)。
    # 只认「设过」的键,不用 tool_default(),否则分不清「设成 read」与「压根没设」。
    tool_defaults = _repo.policy_store.tool_defaults()
    if _repo.tool_id in tool_defaults:
        return tool_defaults[_repo.tool_id]
    # ZE 工具登录后应能创建/编辑自己的项目。全局策略文件历史默认值是 read,但没有显式
    # 给本工具设权限时,不能把正常登录用户锁成只读。
    if _repo.policy_store.default_permission == PERMISSION_BAN:
        return PERMISSION_BAN
    # 兜底档由部署声明(ze.auth.tool_default),不再硬编码 edit ——
    # 标准 skill 部署到公网时应声明 none(默认无权限、须申请),
    # 否则「谁登录都能编辑」这件事只能靠人记得去管理台点一下。超管在上面已经返回过 EDIT。
    _cfg = getattr(_repo, "config", None)
    declared = str((_cfg.get("ze.auth.tool_default", "") if _cfg else "") or "").strip().lower()
    # config 是 KIND_ENUM,取值已被 config 层校验过;这里只做「不认识就回落」的保险,
    # 不引 auth 的私有 _clean_permission(跨模块用私有名会在下次 core 同步时断)。
    # 用已导出的四个常量拼判据,不去 sdk 里加新导出 —— 那是受管层,为一处判断动它的导出面
    # 会让每个下游都跟着变。
    return declared if declared in (
        PERMISSION_NONE, PERMISSION_READ, PERMISSION_EDIT, PERMISSION_BAN) else PERMISSION_EDIT


def _require_tool_access(actor: Actor):
    _require_login(actor)
    user = _repo.user_store.get(actor.user_id)
    if user is None:
        raise ZeError("auth_required", "login required")
    permission = _effective_permission(user)
    if not permission_allows(permission, PERMISSION_READ):
        # none 与 ban 都拒,但文案要能区分:一个可以申请,一个申请也没用。
        raise ZeError(
            "acl_denied",
            "当前用户尚未获得此 ZE 工具的权限,可在工具内提交申请"
            if permission == PERMISSION_NONE
            else "当前用户无权访问此 ZE 工具",
            context={"permission": permission, "can_request": permission == PERMISSION_NONE},
        )
    return user


def _require_edit(actor: Actor):
    user = _require_tool_access(actor)
    permission = _effective_permission(user)
    if permission != PERMISSION_EDIT:
        raise ZeError("read_only_violation", "当前用户是只读权限,不能执行写操作")
    return user


def _require_super_admin(actor: Actor):
    user = _require_tool_access(actor)
    if not is_super_admin(user.user_id):
        raise ZeError("acl_denied", "需要 ZE 超级管理员权限")
    return user


# ---- demo 沙盒访客旁路:人人可操作,改动只落隐藏沙盒(另存才成真项目)----
# DEMO_GUEST_USER_ID 是保留 id:非空 → 过 framework enforce_login(command.py §6,空 user_id
# 抛 auth_required);非真账号 → 不入 user_store,故只能落到 demo_session doc(下方 helper 显式
# 放行),拿它碰真项目仍会在 _require_tool_access(user_store.get → None)处 401。
DEMO_GUEST_USER_ID = "__demo_guest__"
_DEMO_GUEST_ACTOR = Actor(user_id=DEMO_GUEST_USER_ID, source="user")
# demo 沙盒 project id 恒以此前缀(bootstrap._demo_sandbox_id);用作「先校登录再加载」的快判。
DEMO_PROJECT_PREFIX = "__demo__"
# 访客会话 cookie:每浏览器一个随机 guest 身份 → 各自不可枚举的 demo 沙盒(互不干扰、
# 互不可重置/操作)。仅用于 demo 沙盒归属,无任何真账号权限。
GUEST_COOKIE_NAME = "ze_guest"
GUEST_COOKIE_MAX_AGE = 7 * 86400
_GUEST_SCOPE_DOMAIN = b"ze.demo.guest-scope.v1\0"


def _guest_scope(guest_id: str) -> str:
    """Stable browser-session label without exposing the HttpOnly identity.

    Guest ids contain 96 random bits, so a domain-separated SHA-256 digest is
    safe to return to the frontend and lets it bind unresolved intents to the
    same cookie scope without making the cookie itself script-readable.
    """
    return hashlib.sha256(_GUEST_SCOPE_DOMAIN + guest_id.encode("utf-8")).hexdigest()


def _with_project_generation(payload: dict[str, Any], ctx: Context) -> dict[str, Any]:
    """Copy one response/event and fence it to the Context generation."""
    return {**payload, "project_generation": int(ctx.generation)}


def _with_event_epoch(payload: dict[str, Any], ctx: Context) -> dict[str, Any]:
    """Fence a realtime payload to one in-process broker incarnation."""
    return {
        **_with_project_generation(payload, ctx),
        "event_epoch": str(ctx.broker.stats()["stream_id"]),
    }


def _parse_project_generation(raw: Any, *, allow_zero: bool = False) -> int:
    if isinstance(raw, bool):
        raise ZeError("validation_failed", "expected_generation must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ZeError("validation_failed", "expected_generation must be an integer") from exc
    minimum = 0 if allow_zero else 1
    if value < minimum or str(raw).strip() != str(value):
        suffix = "non-negative" if allow_zero else "positive"
        raise ZeError("validation_failed", f"expected_generation must be a {suffix} integer")
    return value


def _request_expected_generation(
    request: Request,
    body: dict[str, Any] | None = None,
    *,
    required: bool = True,
    allow_zero: bool = False,
    default: int | None = None,
) -> int:
    """Normalize the generation CAS from JSON, query, or header.

    Multiple transports may repeat the value, but conflicting values fail
    closed.  Callers remove the private field before command schema dispatch
    and put this normalized integer back into the idempotency request body.
    """
    candidates: list[Any] = []
    if isinstance(body, dict) and "expected_generation" in body:
        candidates.append(body.get("expected_generation"))
    query_params = getattr(request, "query_params", {}) or {}
    headers = getattr(request, "headers", {}) or {}
    if "expected_generation" in query_params:
        candidates.append(query_params.get("expected_generation"))
    for header in ("x-project-generation", "expected-generation"):
        if headers.get(header) is not None:
            candidates.append(headers.get(header))
    if not candidates:
        if default is not None:
            return _parse_project_generation(default, allow_zero=allow_zero)
        if required:
            raise ZeError("validation_failed", "expected_generation is required for project writes")
        raise ZeError("validation_failed", "expected_generation is unavailable")
    values = {
        _parse_project_generation(item, allow_zero=allow_zero)
        for item in candidates
    }
    if len(values) != 1:
        raise ZeError("validation_failed", "conflicting expected_generation values")
    return values.pop()


def _without_expected_generation(body: dict[str, Any]) -> dict[str, Any]:
    clean = dict(body)
    clean.pop("expected_generation", None)
    return clean


def _require_context_generation(ctx: Context, expected_generation: int) -> None:
    if int(ctx.generation) != expected_generation:
        raise ZeError(
            "project_generation_stale",
            "project changed; reload before writing",
            context={
                "project_id": ctx.doc.id,
                "expected_generation": expected_generation,
                "current_generation": int(ctx.generation),
            },
        )


def _is_demo_doc(doc) -> bool:
    """demo 沙盒(project_kind=demo_session):公共可操作、免登录、改动不存为真项目。"""
    return (getattr(doc, "config", None) or {}).get("project_kind") == "demo_session"


def _demo_principal(ctx: "Context", actor: Actor, request: Request) -> str:
    principal = actor.user_id or request.cookies.get(GUEST_COOKIE_NAME) or ""
    if not principal:
        raise ZeError("guest_session_required", "guest session is required")
    owner = str((ctx.doc.acl or {}).get("owner") or "")
    if not owner or principal != owner:
        raise ZeError(
            "acl_denied", "no access to this demo session",
            context={"project_id": ctx.doc.id},
        )
    return principal


def _require_edit_ctx(ctx: "Context", actor: Actor, request: Request) -> Actor:
    """写操作统一闸(须在 ctx 已加载后调用):
    - demo 沙盒 → 放行,返回有效 actor(登录用户用本人,匿名访客用保留 guest);免登录、免写权。
    - 真项目 → 要求登录 + 写权限,返回本人 actor。
    返回的 actor 一定 user_id 非空,可直接喂给 enforce_login 的 dispatch。

    对真正 mutation 的判定必须在 ``ctx.lock`` 内、紧邻执行；锁外调用只能作为
    大请求的廉价预检，不能授权后续写入。否则 owner 可在 caller 排队时撤权，caller
    却仍消费旧 ACL grant。"""
    if _is_demo_doc(ctx.doc):
        principal = _demo_principal(ctx, actor, request)
        return actor if actor.user_id else Actor(user_id=principal, source="user")
    _require_edit(actor)
    if not _can_access(ctx.doc.acl, actor.user_id):
        raise ZeError(
            "acl_denied", "no access to project",
            context={"project_id": ctx.doc.id},
        )
    return actor


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=False,
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", httponly=True, samesite="lax")


def _auth_user_payload(user) -> dict:
    permission = _effective_permission(user) if user else PERMISSION_BAN
    return {
        "id": user.user_id,
        "name": user.display_name or user.user_id,
        "email": user.email or None,
        "avatar_url": user.avatar_url or None,
        "status": user.status,
        "permission": permission,
        "tool_id": _repo.tool_id,
        "is_super_admin": is_super_admin(user.user_id),
    }


def _user_directory_card(user) -> dict:
    return user_public_card(
        user,
        permission=_effective_permission(user),
        tool_id=_repo.tool_id,
    )


def _session_response(token: str, user) -> JSONResponse:
    response = JSONResponse(content={"token": token, "user": _auth_user_payload(user)})
    _set_session_cookie(response, token)
    return response


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _parse_sse_lines(lines):
    event = "message"
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data:
                yield event, "\n".join(data)
            event = "message"
            data = []
            continue
        if line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data:
        yield event, "\n".join(data)


def _json_data(text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"data": text}
    return value if isinstance(value, dict) else {"data": value}


def _larkauth_headers() -> dict[str, str]:
    headers = {"Accept": "text/event-stream"}
    app_id = os.environ.get("ZE_FEISHU_APP_ID") or os.environ.get("X_FEISHU_APP_ID")
    app_secret = os.environ.get("ZE_FEISHU_APP_SECRET") or os.environ.get("X_FEISHU_APP_SECRET")
    if app_id and app_secret:
        headers["X-Feishu-App-Id"] = app_id
        headers["X-Feishu-App-Secret"] = app_secret
    return headers


def _larkauth_url(brand: str | None, logo: str | None, timeout: int | None) -> str:
    params: dict[str, str] = {}
    if brand:
        params["brand"] = brand
    if logo:
        params["logo"] = logo
    if timeout:
        params["timeout"] = str(timeout)
    query = urllib.parse.urlencode(params)
    return f"{LARK_AUTH_BASE_URL}/auth/sse" + (f"?{query}" if query else "")


def _can_access(acl: dict, user_id: str) -> bool:
    """owner 空 = 无主公共(登录用户皆可);否则 owner / collaborator。"""
    if not isinstance(acl, dict):
        return False
    owner = acl.get("owner", "")
    collaborators = acl.get("collaborators", [])
    if (
        not isinstance(owner, str)
        or not isinstance(collaborators, list)
        or any(not isinstance(item, str) for item in collaborators)
    ):
        return False
    if not owner:
        return True
    return user_id == owner or user_id in collaborators


def _member_context_for(project_id: str, actor: Actor, request: Request) -> Context:
    # 真项目:先校登录,匿名绝不触发 get_or_load —— 杜绝匿名暖 cache / 借加载时序探测真项目存在性。
    # 仅 demo 沙盒(__demo__ 前缀)放后加载再判,因 demo 本就免登录人人可开。
    looks_demo = project_id.startswith(DEMO_PROJECT_PREFIX)
    authed = False
    if not looks_demo:
        _require_tool_access(actor)                         # 匿名 → 401;工具 ban → 403
        authed = True
    # 单次 get_or_load 同时完成路径护栏、live-state 与 generation 校验。禁止先
    # project_exists 再 load 的 TOCTOU，也不能把 quiescing/repair 错误吞成 404。
    ctx = _repo.get_or_load(project_id)
    # demo 沙盒免账号登录，但绝不是公共项目：HttpOnly guest cookie / 登录 user
    # 必须严格等于 sandbox ACL owner，两个浏览器不能互读互写。
    if _is_demo_doc(ctx.doc):
        _demo_principal(ctx, actor, request)
        return ctx
    # __demo__ 前缀但实际非 demo(异常/遗留)→ 补校登录,绝不让匿名经空 owner 公共路径误放行。
    if not authed:
        _require_tool_access(actor)
    if not _can_access(ctx.doc.acl, actor.user_id):
        raise ZeError("acl_denied", f"no access to project {project_id!r}",
                      context={"project_id": project_id})
    return ctx


def _idempotency_context_for(
    project_id: str, actor: Actor, request: Request,
) -> tuple[Context, Actor]:
    """Load a generation fence before the request ledger, without weakening ACL.

    A proven response may be replayed after the original command removed its
    actor from the ACL.  New executions still call ``_require_edit_ctx`` inside
    the ledger action.  Demo ledgers are scoped by their secret guest principal,
    never by the shared anonymous Actor id.
    """
    if not project_id.startswith(DEMO_PROJECT_PREFIX):
        _require_tool_access(actor)
    ctx = _repo.get_or_load(project_id)
    if _is_demo_doc(ctx.doc):
        principal = _demo_principal(ctx, actor, request)
        return ctx, actor if actor.user_id else Actor(user_id=principal, source="user")
    _require_tool_access(actor)
    return ctx, actor


def _require_member(
    request: Request,
    project_id: str = Query(...),
    actor: Actor = Depends(_get_actor),
) -> Context:
    """登录 + 是该 project 成员 → 返回其 Context;否则 401/403/404。

    ACL 来源统一为已加载 Context 的内存 `ctx.doc.acl`(单一权威):先 get_or_load
    (路径穿越护栏在内),再据内存 acl 判权 —— 避免「磁盘 project_meta 判权 / 内存 acl
    执行」两源不一致(set_acl 改完未落盘时旧盘 acl 误放行已移除的协作者)。
    """
    return _member_context_for(project_id, actor, request)


def _require_member_leased(
    request: Request,
    project_id: str = Query(...),
    actor: Actor = Depends(_get_actor),
) -> Iterator[Context]:
    """Hold the current generation for one bounded HTTP read/write request.

    Long-lived SSE and AI calls deliberately use ``_require_member`` and do
    their own short generation checks; otherwise one idle connection could
    block project reset/delete forever.
    """
    ctx = _member_context_for(project_id, actor, request)
    with ctx.lifecycle_guard():
        yield ctx


def _require_transport_member(
    request: Request,
    project_id: str = Query(...),
    actor: Actor = Depends(_get_transport_actor),
) -> Context:
    """Member dependency for EventSource/media URLs with an explicit token."""
    return _member_context_for(project_id, actor, request)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ==============================================================
# 认证 REST(无 project;不经 command/Operation 通道)
# ==============================================================

@app.get("/api/auth/config")
def auth_config():
    """返回浏览器可见的登录能力；不返回密钥、路径或 token。"""
    return JSONResponse(
        content=auth_runtime_config().public_dict(),
        headers={"Cache-Control": "no-store"},
    )

@app.post("/api/auth/register")
async def auth_register(request: Request):
    config = auth_runtime_config()
    if not config.allows_local:
        raise ZeError("auth_method_disabled", "local account login is disabled")
    if not config.registration_open:
        raise ZeError("registration_closed", "account registration is closed")
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body must be JSON object")
    username = normalize_local_login_name(
        body.get("username", ""),
        login_name_format=config.login_name_format,
    )
    user = _repo.user_store.register(username, body.get("password", ""))
    user = _repo.user_store.record_login(user.user_id, tool_id=_repo.tool_id) or user
    token = _repo.session_mgr.login(user)
    _repo.auth_audit.append(
        "login", target_user_id=user.user_id, tool_id=_repo.tool_id, source="password_register")
    _usage.emit("session_start", anon=anon_user(user.user_id))
    return _session_response(token, user)


@app.post("/api/auth/login")
async def auth_login(request: Request):
    config = auth_runtime_config()
    if not config.allows_local:
        raise ZeError("auth_method_disabled", "local account login is disabled")
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body must be JSON object")
    username = normalize_local_login_name(
        body.get("username", ""),
        login_name_format=config.login_name_format,
    )
    user = _repo.user_store.authenticate(username, body.get("password", ""))
    user = _repo.user_store.record_login(user.user_id, tool_id=_repo.tool_id) or user
    token = _repo.session_mgr.login(user)
    _repo.auth_audit.append(
        "login", target_user_id=user.user_id, tool_id=_repo.tool_id, source="password")
    _usage.emit("session_start", anon=anon_user(user.user_id))
    return _session_response(token, user)


@app.get("/api/auth/lark/sse")
def auth_lark_sse(
    brand: Optional[str] = Query(default="ZE"),
    logo: Optional[str] = Query(default=None),
    timeout: Optional[int] = Query(default=300, ge=60, le=600),
):
    """飞书 OAuth 登录:ZE 后端代理 larkauth SSE,成功后签发本地 session。

    前端只连接同源 `/api/auth/lark/sse`;飞书 App Secret 只允许通过服务端环境变量
    `ZE_FEISHU_APP_ID`/`ZE_FEISHU_APP_SECRET` 注入,绝不暴露给浏览器。
    """

    config = auth_runtime_config()
    if not config.allows_feishu:
        raise ZeError("auth_method_disabled", "Feishu login is disabled")

    def stream():
        url = _larkauth_url(brand, logo, timeout)
        req = urllib.request.Request(url, headers=_larkauth_headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=(timeout or 300) + 15) as resp:
                for event, data_text in _parse_sse_lines(resp):
                    data = _json_data(data_text)
                    if event == "auth_url":
                        auth_url = data.get("url")
                        if not isinstance(auth_url, str) or not auth_url:
                            yield _sse("auth_error", {"error": "飞书认证服务返回了无效授权链接"})
                            return
                        yield _sse("auth_url", {"url": auth_url})
                    elif event == "user_info":
                        open_id = data.get("open_id")
                        if not isinstance(open_id, str) or not open_id.strip():
                            yield _sse("auth_error", {"error": "飞书认证服务未返回 open_id"})
                            return
                        user = _repo.user_store.upsert_external_user(
                            provider="feishu",
                            external_id=open_id,
                            display_name=str(data.get("name") or ""),
                            email=str(data.get("email") or ""),
                            avatar_url=str(data.get("avatar_url") or ""),
                            tool_id=_repo.tool_id,
                        )
                        token = _repo.session_mgr.login(user)
                        _repo.auth_audit.append(
                            "login", target_user_id=user.user_id, tool_id=_repo.tool_id,
                            source="feishu")
                        _usage.emit("session_start", anon=anon_user(user.user_id))
                        yield _sse("session", {"token": token, "user": _auth_user_payload(user)})
                        return
                    elif event == "error":
                        yield _sse("auth_error", {"error": data.get("error") or "飞书认证失败"})
                        return
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            yield _sse("auth_error", {"error": f"无法连接飞书认证服务:{exc}"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/auth/session-cookie")
async def auth_session_cookie(request: Request):
    """把 SSE 登录签发的 token 固化为全 ZE 系列共享 HttpOnly cookie。"""
    body = await request.json()
    token = str((body or {}).get("token") or "") if isinstance(body, dict) else ""
    user_id = _repo.session_mgr.get_user_id(token)
    if not user_id:
        raise ZeError("auth_failed", "invalid session")
    user = _repo.user_store.get(user_id)
    if user is None:
        raise ZeError("auth_failed", "invalid session")
    return _session_response(token, user)


@app.post("/api/auth/logout")
async def auth_logout(request: Request, token: Optional[str] = Query(default=None)):
    tok = _request_token(request, token)
    if tok:
        user_id = _repo.session_mgr.get_user_id(tok) or ""
        _repo.session_mgr.logout(tok)
        if user_id:
            _repo.auth_audit.append(
                "logout", actor_user_id=user_id, target_user_id=user_id,
                tool_id=_repo.tool_id, source="http")
    response = JSONResponse(content={"ok": True})
    _clear_session_cookie(response)
    return response


@app.get("/api/auth/me")
def auth_me(actor: Actor = Depends(_get_actor)):
    user = _require_tool_access(actor)
    return {"user": _auth_user_payload(user)}


@app.get("/api/auth/users")
def auth_users(actor: Actor = Depends(_get_actor)):
    """列已注册 user_id(供共享时校验/补全协作者)。只返 id,绝不返 hash。"""
    _require_edit(actor)
    return {"users": [u.user_id for u in _repo.user_store.list_users() if u.status != USER_STATUS_DISABLED]}


@app.get("/api/auth/user-directory")
def auth_user_directory(
    query: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=100),
    channel: str = Query(default=""),
    actor: Actor = Depends(_get_actor),
):
    """Search active ZE users from the server-side ZE-system identity directory."""
    _require_tool_access(actor)
    users = search_users(
        _repo.user_store.list_users(),
        query=query,
        limit=limit,
        channel=channel,
    )
    return {"users": [_user_directory_card(u) for u in users]}


@app.post("/api/auth/user-directory/lookup")
async def auth_user_directory_lookup(request: Request, actor: Actor = Depends(_get_actor)):
    """Resolve stored user_id references into display cards without exposing secrets."""
    _require_tool_access(actor)
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body must be JSON object")
    raw_ids = body.get("ids")
    if not isinstance(raw_ids, list):
        raise ZeError("validation_failed", "ids must be a list")
    channel = str(body.get("channel") or "")
    ids = [str(x) for x in raw_ids if isinstance(x, (str, int, float))]
    users, missing = lookup_users(_repo.user_store.list_users(), ids, channel=channel)
    return {"users": [_user_directory_card(u) for u in users], "missing": missing}


# ==============================================================
# 当前账号绑定 REST(外部 API Key / Codex / Claude profile)
# ==============================================================

def _ensure_mod_code_path() -> None:
    mod_dir = str(Path(__file__).resolve().parent / "02-mod" / "05-code")
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)


def _cursor_cred_mod():
    """本工具 Cursor 私有凭据分流(02-mod);不碰 Hub。"""
    _ensure_mod_code_path()
    import cursor_credentials as mod  # noqa: WPS433
    return mod


def _credential_provider_cards() -> list[dict[str, Any]]:
    """Provider catalog for the account dialog; no secrets."""
    labels = {
        PROVIDER_HUPO_GATEWAY: "Hupo / OpenAI-compatible gateway",
        PROVIDER_OPENAI_API: "OpenAI API key",
        PROVIDER_ANTHROPIC_API: "Anthropic API key",
        PROVIDER_CURSOR_API: "Cursor API key",
        PROVIDER_GEMINI_API: "Gemini API key",
        PROVIDER_CODEX_CLI: "Codex CLI profile",
        PROVIDER_CLAUDE_CLI: "Claude CLI profile",
        PROVIDER_GITHUB_REPO: "GitHub repository",
    }
    help_text = {
        PROVIDER_HUPO_GATEWAY: "填写个人 Hupo NewAPI Key;Base URL 只允许官方 HTTPS 网关。",
        PROVIDER_OPENAI_API: "填写 OpenAI API Key;Base URL 只允许 api.openai.com。",
        PROVIDER_ANTHROPIC_API: "填写 Anthropic API Key;Base URL 只允许 api.anthropic.com。",
        PROVIDER_CURSOR_API: "填写你自己的 Cursor User API Key;仅本工具(视频编辑再生成)有效,不写入全站 Hub。调用计入该用户的 Cursor 计划与用量池。",
        PROVIDER_GEMINI_API: "填写 Google AI Studio 的 Gemini API Key(免费创建,约 500 张/天生图额度)。绑定时会做一次最小校验(models.list,零消耗),坏 key 绑不进去。可驱动:对话 / 生图。",
        PROVIDER_CODEX_CLI: "绑定你自己的 Codex 订阅账号(供浮层 AI 用你的额度)。首选设备登录:在弹窗里打开授权链接、在本地浏览器批准即可;也可粘贴 ~/.codex/auth.json 全文。",
        PROVIDER_CLAUDE_CLI: "绑定你自己的 Claude 订阅账号(供浮层 AI 用你的额度)。首选设备登录:在弹窗里打开授权链接、在本地浏览器批准即可;也可粘贴 ~/.claude/.credentials.json 全文。",
        PROVIDER_GITHUB_REPO: "关联一个 GitHub 仓库供 AI 只读:填 owner/repo(可选 ref 分支),并绑定一枚个人访问令牌(PAT)。令牌只在服务端密封保存,绝不回显。",
    }
    return [{"id": p, "label": labels[p], "help": help_text[p]} for p in PROVIDERS]


def _bind_cli_home(user_id: str, rec, auth_file: str) -> Any:
    """Provision the per-user isolated CLI home, stamp its path on the record,
    and (if a full credential file was pasted) write it into that home.

    Returns the up-to-date record. Only for CLI providers (claude/codex)."""
    home = provision_cli_home(user_id, rec.provider, rec.id)
    if auth_file:
        write_cli_auth_file(home, rec.provider, auth_file)
    # Stamp the home path so the agentic runner can export it as
    # CLAUDE_CONFIG_DIR / CODEX_HOME. Re-upsert merges metadata.
    return _repo.credential_store.upsert_for_user(
        user_id,
        credential_id=rec.id,
        provider=rec.provider,
        label=rec.label,
        metadata={META_HOME_DIR: str(home)},
    )


def _credential_public(rec) -> dict[str, Any]:
    return rec.public_dict() if hasattr(rec, "public_dict") else dict(rec)


_CREDENTIAL_UNKNOWN_CODES = (
    "credential_store_durability_unknown",
    "credential_key_durability_unknown",
    "credential_cli_auth_durability_unknown",
)
_CREDENTIAL_USER_METADATA = {
    PROVIDER_HUPO_GATEWAY: frozenset(("base_url",)),
    PROVIDER_OPENAI_API: frozenset(("base_url",)),
    PROVIDER_ANTHROPIC_API: frozenset(("base_url",)),
    PROVIDER_CODEX_CLI: frozenset(("account", "profile")),
    PROVIDER_CLAUDE_CLI: frozenset(("account", "profile")),
    PROVIDER_GITHUB_REPO: frozenset((META_REPO_OWNER, META_REPO_NAME, META_REPO_REF)),
}
_CREDENTIAL_ENDPOINT_HOSTS = {
    PROVIDER_HUPO_GATEWAY: frozenset(("api.ai.hupo.games",)),
    PROVIDER_OPENAI_API: frozenset(("api.openai.com",)),
    PROVIDER_ANTHROPIC_API: frozenset(("api.anthropic.com",)),
}


def _credential_base_url(provider: str, value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ZeError("validation_failed", "credential base_url is invalid") from exc
    allowed = _CREDENTIAL_ENDPOINT_HOSTS.get(provider, frozenset())
    is_official = bool(parsed.hostname) and parsed.hostname.lower() in allowed
    if is_official:
        # 官方 host:沿用原严格检查(https/标准端口/无 userinfo/query),行为不变。
        if (
            parsed.scheme.lower() != "https"
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.query)
            or bool(parsed.fragment)
        ):
            raise ZeError(
                "validation_failed",
                "credential base_url is outside the provider HTTPS allowlist",
            )
        return raw
    # 官方 host 之外 → P5 用户自填端点护栏(is_safe_registerable_endpoint 已含 https/私网·环回·链路本地/
    # userinfo/query/非标端口 全检查)。通过=登记为该凭据的自定义端点;运行时出站再经 guard_outbound_host
    # 解析核验(防 DNS rebinding)。自由登记=向白名单加带护栏条目,非绕过白名单(§P5 教义③)。
    from sdk import hub  # noqa: E402  (懒引受管护栏)
    safe, reason = hub.is_safe_registerable_endpoint(raw)
    if not safe:
        raise ZeError(
            "validation_failed",
            f"credential base_url rejected by endpoint safety guard: {reason}",
        )
    return raw


def _credential_metadata_from_request(provider: str, raw: Any) -> dict[str, Any]:
    """Validate the public DTO; internal metadata never comes from the browser.

    ``home_dir`` and ``is_default`` are server-owned invariants.  Token/secret
    shaped extension fields are rejected instead of being persisted in clear
    text and echoed by a later API version.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ZeError("validation_failed", "credential metadata must be an object")
    allowed = _CREDENTIAL_USER_METADATA.get(provider, frozenset())
    extra = sorted(str(key) for key in raw if key not in allowed)
    if extra:
        raise ZeError(
            "validation_failed",
            "credential metadata contains server-owned or unsupported fields",
            context={"fields": extra},
        )
    out = {str(key): value for key, value in raw.items()}
    if "base_url" in out:
        out["base_url"] = _credential_base_url(provider, out["base_url"])
    if provider == PROVIDER_GITHUB_REPO:
        # owner/repo/ref 是关联本身(非密文);在入口就按 GitHub 命名规则校验,
        # 防注入进后续 contents API URL(客户端 read_path 再做一次纵深防御)。
        if META_REPO_OWNER in out:
            out[META_REPO_OWNER] = github_validate_owner(out[META_REPO_OWNER])
        if META_REPO_NAME in out:
            out[META_REPO_NAME] = github_validate_repo(out[META_REPO_NAME])
        if META_REPO_REF in out:
            ref = github_validate_ref(out[META_REPO_REF])
            if ref:
                out[META_REPO_REF] = ref
            else:
                out.pop(META_REPO_REF, None)
    return out


@app.get("/api/account/credentials")
def account_credentials(actor: Actor = Depends(_get_actor)):
    """List current user's external account bindings; metadata only, never secrets."""
    _require_tool_access(actor)
    rows = _cursor_cred_mod().list_merged_for_user(
        _repo.credential_store, actor.user_id,
    )
    return {
        "providers": _credential_provider_cards(),
        "credentials": [_credential_public(r) for r in rows],
    }


@app.get("/api/account/cursor-models")
def account_cursor_models(actor: Actor = Depends(_get_actor)):
    """Account-specific Cursor catalog; the sealed API key never leaves the server."""
    _require_tool_access(actor)
    cursor_store = _cursor_cred_mod().cursor_credential_store()
    with credential_context(actor, cursor_store, _repo.policy_store, _repo.tool_id):
        return {"models": cursor_models_for_current_user(credential_store=cursor_store)}


@app.get("/api/ai-routes")
def ai_routes(actor: Actor = Depends(_get_actor)):
    """AI 通道可用性(3.56「没绑就先说」不变量的数据源):按当前用户返回每条生图路线
    {route, available, reason, fix}。reason 枚举 not_bound / not_configured / version_blocked;
    探活类(service_unreachable / quota_low)不在此做——列表接口不打外网,真调用时结构化报错。

    访客(demo/未登录)也放行为**只读路线态**:无账号=无凭据=所有路线不可用。返回可门控数据
    (而非 401)让前端 AiRouteGate 在点生成前就置灰+引导连接——否则 401 使 aiRoutes 未加载、
    门透传,访客能点生成再逐张报错(评估报告 C 端空态缺口)。路线可用性非敏感;封禁等非
    「未登录」错误仍拒。"""
    try:
        _require_tool_access(actor)
    except ZeError as _access_err:
        if _access_err.code != "auth_required":
            raise
    from credentials import DEFAULT_CAPABILITIES as _CAPS
    rows = _repo.credential_store.list_for_user(actor.user_id) if actor.user_id else []
    bound = {r.provider for r in rows if "image" in (r.capabilities or _CAPS.get(r.provider, ()))}
    out = []
    service_ok = bool(str(_repo.config.get("ze.image.service_url", "") or "").strip())
    out.append({"route": "service", "available": service_ok,
                "reason": None if service_ok else "not_configured",
                "fix": None if service_ok else {"action": "configure", "key": "ze.image.service_url"}})
    for provider, route in ((PROVIDER_CODEX_CLI, "codex-cli"),
                            (PROVIDER_OPENAI_API, "openai-api"),
                            (PROVIDER_GEMINI_API, "gemini-api")):
        ok = provider in bound
        out.append({"route": route, "available": ok,
                    "reason": None if ok else "not_bound",
                    "fix": None if ok else {"action": "bind", "provider": provider,
                                            "capability": "image"}})
    current = str(_repo.config.get("ze.image.route", "service") or "service")
    if current == "auto":
        # auto 是一等公民「元路线」(image_call → _pick_image_route):内网服务已配即用之,
        # 否则挑第一条已绑个人路线。前端 AiRouteGate 需要它自身的可用性才能门控——
        # 否则 routes 无 'auto' 行 → find 落空 → 无条件透传,auto 态下「没绑就先说」失效。
        auto_ok = service_ok or bool(bound)
        out.append({"route": "auto", "available": auto_ok,
                    "reason": None if auto_ok else "not_bound",
                    "fix": None if auto_ok else {"action": "bind", "provider": PROVIDER_CODEX_CLI,
                                                 "capability": "image"}})
    return {"routes": out, "current": current}


@app.get("/api/ai/options")
def ai_options(
    feature_id: str = Query(default="generic"),
    capability: str = Query(default="image"),
    require: str = Query(default=""),
    pin: str = Query(default=""),
    intent: str = Query(default=""),
    preference: str = Query(default=""),
    actor: Actor = Depends(_get_actor),
):
    """ProviderHub 能力选项(规划 §3.6 CapabilityModelPicker 的数据源)—— /api/ai-routes 的
    严格超集:按 feature/capability/require 返当前用户可用**模型**投影(EffectiveCapabilitySnapshot),
    每个候选带可用性 + 9 类统一错误码 + fix + i18n key,options 恒含全部候选(选择器显示所有
    可能性、只区别显示能用的)。

    唯一解析算法在 sdk.hub.resolve(纯函数);这里只做接线(取连接 public 投影 + service/audience
    配置)。访客同 /api/ai-routes 放行为只读态(无连接=所有候选 requires_connection),不返 401,
    让前端在点击前门控。pin 不可用由 resolver 硬阻断(current=None),不静默换模型。"""
    try:
        _require_tool_access(actor)
    except ZeError as _access_err:
        if _access_err.code != "auth_required":
            raise
    from sdk import hub  # noqa: E402  (受管 ProviderHub 内核; 懒引同 /api/ai-routes)
    rows = _repo.credential_store.list_for_user(actor.user_id) if actor.user_id else []
    conns = hub.project_connections([r.public_dict() for r in rows])
    service_ok = bool(str(_repo.config.get("ze.image.service_url", "") or "").strip())
    # audience 由 Deploy profile 决定(§5.4 public-consumer 隐藏内网服务); 缺省 internal 兼容本地/组织。
    # C 端 public 部署应配 ze.ai.audience=public, 使内网服务路线在公网不可见(§4.3)。
    audience = str(_repo.config.get("ze.ai.audience", hub.AUDIENCE_INTERNAL) or hub.AUDIENCE_INTERNAL)
    requirement = hub.CapabilityRequirement(
        feature_id=feature_id or "generic",
        capability=capability or "image",
        require=tuple(r for r in (require or "").replace(",", " ").split() if r),
    )
    # §3.4 FeaturePreference:query 显式 preference 优先,否则读用户存的私有默认(best-effort,不阻断)。
    eff_pref = preference or ""
    eff_lane: tuple = ()
    if actor.user_id:
        if not eff_pref:
            _pstore = getattr(_repo, "preference_store", None)
            if _pstore is not None:
                try:
                    eff_pref = _pstore.get(actor.user_id, feature_id or "generic", capability or "image") or ""
                except Exception:  # noqa: BLE001 — 偏好读失败不阻断选项
                    eff_pref = ""
        # §3.4 CapabilityLane:用户在该能力下的全局优先序(resolver 五级优先第三层,preference 之后)。
        _lstore = getattr(_repo, "lane_store", None)
        if _lstore is not None:
            try:
                eff_lane = _lstore.get(actor.user_id, capability or "image")
            except Exception:  # noqa: BLE001
                eff_lane = ()
    # §3.5 步5 健康冷却:从用户近期 InvocationReceipt 算出连续失败的模型 → 解析期临时置灰(best-effort)。
    eff_cooldown: dict = {}
    if actor.user_id:
        _ilog = getattr(_repo, "invocation_log", None)
        if _ilog is not None:
            try:
                from sdk.hub.cooldown import cooldown_reasons
                _recent = [r.public_dict() for r in _ilog.recent(50, user_id=actor.user_id)]
                eff_cooldown = cooldown_reasons(_recent, now=time.time())
            except Exception:  # noqa: BLE001 — 冷却读失败不阻断选项
                eff_cooldown = {}
    snap = hub.resolve(
        requirement, conns, service_configured=service_ok,
        pin=pin or None, intent=intent or None, preference=eff_pref or None, lane=eff_lane,
        cooldown=eff_cooldown or None, deploy_audience=audience,
    )
    return hub.snapshot_public_dict(snap)


@app.post("/api/ai/preference")
async def ai_set_preference(request: Request, actor: Actor = Depends(_get_actor)):
    """§3.4 设/清用户私有默认模型(FeaturePreference)。body: {feature_id, capability, model_id}
    model_id 空 = 清除(回到 auto/意图)。用户级、不进项目文档、无 secret。访客(空/'__' uid)no-op 返 ok。"""
    _require_tool_access(actor)
    pstore = getattr(_repo, "preference_store", None)
    if pstore is None:
        raise ZeError("validation_failed", "preference store 未启用", context={})
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body must be JSON object")
    feature_id = str(body.get("feature_id") or "generic")
    capability = str(body.get("capability") or "image")
    model_id = str(body.get("model_id") or "")
    pstore.set(actor.user_id, feature_id, capability, model_id)   # 空 model_id → clear;访客 no-op
    return {"ok": True, "feature_id": feature_id, "capability": capability,
            "model_id": pstore.get(actor.user_id, feature_id, capability) or ""}


@app.get("/api/ai/preferences")
def ai_list_preferences(actor: Actor = Depends(_get_actor)):
    """当前用户全部私有默认(设置页/诊断用)。访客 → 空。"""
    try:
        _require_tool_access(actor)
    except ZeError as _e:
        if _e.code != "auth_required":
            raise
    pstore = getattr(_repo, "preference_store", None)
    if pstore is None or not actor.user_id or actor.user_id.startswith("__"):
        return {"preferences": {}}
    return {"preferences": pstore.list_for_user(actor.user_id)}


@app.post("/api/ai/lane")
async def ai_set_lane(request: Request, actor: Actor = Depends(_get_actor)):
    """§3.4 设/清用户能力泳道(全局模型优先序)。body: {capability, models: [model_id...]}
    models 空 = 清除。用户级、跨工具、不进项目文档、无 secret。访客 no-op。"""
    _require_tool_access(actor)
    lstore = getattr(_repo, "lane_store", None)
    if lstore is None:
        raise ZeError("validation_failed", "lane store 未启用", context={})
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body must be JSON object")
    capability = str(body.get("capability") or "image")
    models = body.get("models") or []
    if not isinstance(models, list):
        raise ZeError("validation_failed", "models 须为数组")
    lstore.set(actor.user_id, capability, [str(m) for m in models])   # 空 → clear;访客 no-op
    return {"ok": True, "capability": capability, "models": list(lstore.get(actor.user_id, capability))}


@app.get("/api/ai/lanes")
def ai_list_lanes(actor: Actor = Depends(_get_actor)):
    """当前用户全部能力泳道(设置页/诊断用)。访客 → 空。"""
    try:
        _require_tool_access(actor)
    except ZeError as _e:
        if _e.code != "auth_required":
            raise
    lstore = getattr(_repo, "lane_store", None)
    if lstore is None or not actor.user_id or actor.user_id.startswith("__"):
        return {"lanes": {}}
    return {"lanes": lstore.list_for_user(actor.user_id)}


@app.post("/api/ai/discover")
async def ai_discover_models(request: Request, actor: Actor = Depends(_get_actor)):
    """ProviderHub 回收 P4:对已登记凭据拉 provider /models **动态发现**模型,分层落
    DiscoveredModelStore(source=discovered/caps=[],**不进 canonical、不喂 resolver**,教义①)。
    body: {credential_id}。reveal_secret 仅在**确认可发现后**本次注入鉴权;目标 host 经
    _credential_base_url 白名单校验(SSRF);官方 CLI/内网服务无用户端点 → 空(不解密)。访客 no-op。"""
    _require_tool_access(actor)
    from sdk import hub  # noqa: E402  (懒引受管内核)
    store = getattr(_repo, "discovered_model_store", None)
    if store is None:
        raise ZeError("validation_failed", "discovered model store 未启用", context={})
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body must be JSON object")
    credential_id = str(body.get("credential_id") or "")
    cred_store, rec = _cursor_cred_mod().resolve_store_for_credential(
        _repo.credential_store, actor.user_id, credential_id,
    )
    desc = hub.descriptor_for_provider(rec.provider)
    kind = desc.kind if desc else ""
    policy = (desc.endpoint_policy or {}) if desc else {}
    base_url = (_credential_base_url(rec.provider, rec.metadata.get("base_url"))
                or _PROBE_DEFAULT_BASE.get(rec.provider))
    if not base_url or not hub.is_probeable(kind):        # 官方登录/内网服务 → 不解密
        return {"ok": True, "discovered": [], "detail": "该来源无用户可发现端点(官方登录或内网服务)"}
    _guard_ai_outbound(rec.provider, base_url)            # P5:custom host 出站前解析核验(官方放行);失败即拒,不解密
    disc_path = policy.get("discovery_path") or policy.get("test_path") or "/v1/models"
    secret = cred_store.reveal_secret(actor.user_id, credential_id)
    reqspec = hub.build_probe_request(kind, base_url, secret, test_path=disc_path)
    if reqspec is None:
        return {"ok": True, "discovered": []}
    url, headers = reqspec
    ids, error_code = hub.discover_models(url, headers=headers, kind=kind)
    if error_code:
        return {"ok": False, "error_code": error_code, "discovered": []}
    entries = store.record(actor.user_id, credential_id, ids)   # 覆盖该 credential 发现集
    return {"ok": True, "discovered": entries, "count": len(entries)}


@app.get("/api/ai/discovered")
def ai_list_discovered(actor: Actor = Depends(_get_actor)):
    """当前用户全部动态发现模型 {credential_id: [entry...]}(设置页/诊断用)。访客 → 空。"""
    try:
        _require_tool_access(actor)
    except ZeError as _e:
        if _e.code != "auth_required":
            raise
    store = getattr(_repo, "discovered_model_store", None)
    if store is None or not actor.user_id or actor.user_id.startswith("__"):
        return {"discovered": {}}
    return {"discovered": store.list_for_user(actor.user_id)}


@app.get("/api/ai/receipts")
def ai_receipts(limit: int = Query(default=20), actor: Actor = Depends(_get_actor)):
    """当前用户最近的 AI 调用回执(§2.2「当前工具最近调用的实际 provider/model」/ §3.1 诊断页数据源)。

    读独立 InvocationLog(ProviderHub §3.4;不进 Operation 栈),按当前用户过滤 + 脱敏 public_dict
    (无 secret)。访客(空 uid / '__' 前缀)→ 空(复刻凭据访客门)。受众=诊断/最近调用展示,**非
    计费权威**(计费由 Commerce 从脱敏事件汇总)。访客放行只读态(非 401),同 /api/ai/options。"""
    try:
        _require_tool_access(actor)
    except ZeError as _access_err:
        if _access_err.code != "auth_required":
            raise
    log = getattr(_repo, "invocation_log", None)
    uid = actor.user_id
    if log is None or not uid or uid.startswith("__"):
        return {"receipts": []}
    n = max(0, min(int(limit or 0), 100))
    return {"receipts": [r.public_dict() for r in log.recent(n, user_id=uid)]}


@app.get("/api/ai/turns")
def ai_turns(limit: int = Query(default=20), turn_id: str = Query(default=""),
             actor: Actor = Depends(_get_actor)):
    """当前用户最近的助手 Turn(§7.1 协议存储读半)。断线重连恢复 + 「本轮 AI 改了什么」审计
    (change_set 事件里的 operation_ids 即 P0-D 整轮撤销的可逆单位)。

    传 turn_id → 返回该轮完整状态 + 事件流(且必须归属当前用户,杜绝越权读他人 turn);否则
    返回当前用户最近 N 轮(仅 turn 头,不展开 events,轻)。脱敏走 AssistantTurn.public_dict()
    (无 secret、载荷为摘要)。访客(空 uid / '__' 前缀)→ 空,同 /api/ai/receipts 只读放行(非 401)。"""
    try:
        _require_tool_access(actor)
    except ZeError as _access_err:
        if _access_err.code != "auth_required":
            raise
    ts = getattr(_repo, "turn_store", None)
    uid = actor.user_id
    if ts is None or not uid or uid.startswith("__"):
        return {"turns": []} if not turn_id else {"turn": None}
    if turn_id:
        t = ts.get_turn(turn_id)
        if t is None or t.actor_user_id != uid:   # 不存在 / 非本人 → 一律 None(不泄漏存在性)
            return {"turn": None}
        return {"turn": t.public_dict()}
    n = max(0, min(int(limit or 0), 100))
    rows = ts.recent(n, actor_user_id=uid)
    # 列表态只出 turn 头(省 events 展开):前端拿头,点开再按 turn_id 拉全量。
    return {"turns": [{"turn_id": t.turn_id, "feature_id": t.feature_id, "state": t.state,
                       "created_at": t.created_at, "updated_at": t.updated_at,
                       "event_count": len(t.events)} for t in rows]}


def _turn_changeset_data(turn) -> "dict | None":
    """取一轮的 change_set 事件载荷(reversible/irreversible/entities/operation_ids);无则 None。"""
    for e in getattr(turn, "events", ()):
        if e.kind == "assistant_change_set":
            return e.data
    return None


def _undo_guard_holds(doc, guard: dict) -> bool:
    """校验一条补偿 guard 在当前 doc 是否成立(§7.4 并发冲突检测:该 op 效果此刻仍在、未被覆盖)。"""
    et, eid = guard.get("entity_type"), guard.get("entity_id")
    ent = doc.find(et, eid)
    kind = guard.get("kind")
    if kind == "entity_exists":
        return ent is not None
    if kind == "field_unchanged":
        if ent is None:
            return False                                  # 实体已不在 → 不能补偿字段
        field = guard.get("field")
        current = ent.name if field == "name" else ent.get(field)
        return current == guard.get("expect_after")       # 未被后续改动覆盖才安全补偿
    return False


def _plan_turn_undo_locked(ctx, reversible_ids: list) -> dict:
    """规划 + 逐项校 guard(**假定 caller 已持 ctx.lock**)。分 will_revert(guard 全成立)/
    conflict(被后续改动覆盖)/ skipped(类型不支持)。纯读,不改 doc。执行端点持锁调此以保原子。"""
    from sdk.turn_undo import plan_turn_compensation
    want = set(reversible_ids or [])
    # op_log 无 by-id:过滤 entries(append 序)取目标 op,再反成新→旧
    rows = [e["op"] for e in ctx.op_log.entries()
            if isinstance(e, dict) and isinstance(e.get("op"), dict) and e["op"].get("id") in want]
    rows.reverse()
    planned = plan_turn_compensation(rows)
    will_revert, conflict = [], []
    for item in planned["plan"]:
        if item["action"] != "compensate":
            continue
        if all(_undo_guard_holds(ctx.doc, g) for g in item.get("guards", [])):
            will_revert.append({"op_id": item["op_id"], "command_id": item["command_id"],
                                "undoes": item.get("undoes", "")})
        else:
            conflict.append({"op_id": item["op_id"],
                             "reason": "该实体在本轮后被改动,撤销会覆盖后续改动,已跳过"})
    return {"will_revert": will_revert, "conflict": conflict, "skipped": planned["skipped"],
            "plan": planned["plan"]}


def _plan_turn_undo(ctx, reversible_ids: list) -> dict:
    """预览用:自持 ctx.lock 跑一次规划+guard 校验(只读)。"""
    with ctx.lock:
        return _plan_turn_undo_locked(ctx, reversible_ids)


def _undo_compensation_args(ctx, plan_item: dict) -> dict:
    """给补偿命令补 expected_version(从当前实体读,dispatch 时新鲜取,CAS 用)。"""
    args = dict(plan_item["args"])
    ent = ctx.doc.find(args.get("type"), args.get("id"))
    if ent is not None:
        args["expected_version"] = ent.version
    return args


@app.get("/api/ai/turn/{turn_id}/undo-preview")
def ai_turn_undo_preview(turn_id: str, actor: Actor = Depends(_get_actor)):
    """§7.4 P0-D 整轮撤销**预览**(只读):列出本轮将撤销(will_revert)/ 因后续改动冲突跳过(conflict)/
    类型不支持跳过(skipped)/ 不可逆外部效果(irreversible,已花额度不撤只展示)。是「明确列出将撤销和
    不能撤销的部分」的数据源;真正执行走 POST 版(下一片)。访客/无 project/无 change_set → 空。"""
    try:
        _require_tool_access(actor)
    except ZeError as _e:
        if _e.code != "auth_required":
            raise
    ts = getattr(_repo, "turn_store", None)
    uid = actor.user_id
    empty = {"will_revert": [], "conflict": [], "skipped": [], "irreversible": [], "entities": []}
    if ts is None or not uid or uid.startswith("__"):
        return empty
    t = ts.get_turn(turn_id)
    if t is None or t.actor_user_id != uid:               # 不存在/非本人 → 空(不泄漏存在性)
        return empty
    cs = _turn_changeset_data(t)
    if cs is None or not t.project_id:
        return empty
    try:
        ctx = _repo.get_or_load(t.project_id)
    except ZeError:                                       # 项目已删/不可达 → 只回 change_set 静态视图
        return {**empty, "irreversible": cs.get("irreversible", []), "entities": cs.get("entities", [])}
    planned = _plan_turn_undo(ctx, cs.get("reversible", []))
    return {"will_revert": planned["will_revert"], "conflict": planned["conflict"],
            "skipped": planned["skipped"], "irreversible": cs.get("irreversible", []),
            "entities": cs.get("entities", [])}


@app.post("/api/ai/turn/{turn_id}/undo")
async def ai_turn_undo(turn_id: str, request: Request, actor: Actor = Depends(_get_actor)):
    """§7.4 P0-D 整轮撤销**执行**:对本轮可逆 op 逐个 dispatch 补偿正向命令(Form B,source='user',
    经普通提交路径 → CAS 并发安全、可再撤)。全程持 ctx.lock 保原子。冲突(被后续改动覆盖)/ 类型
    不支持 / 不可逆外部效果(已花额度)均跳过并列出(§7.4 明确列出将撤销和不能撤销的部分)。

    注:多 op 改同一实体时保守只撤最新那条(其余标 conflict),避免部分补偿留中间态;撤销本身
    不要求 client expected_generation(server 持锁批量),当前多写原子由 ctx.lock 保证。"""
    _require_tool_access(actor)
    ts = getattr(_repo, "turn_store", None)
    uid = actor.user_id
    t = ts.get_turn(turn_id) if (ts is not None and uid and not uid.startswith("__")) else None
    if t is None or t.actor_user_id != uid:               # 不存在/非本人 → 一律 not_found(不泄漏存在性)
        raise ZeError("entity_not_found", "turn 不存在或非本人", context={})
    cs = _turn_changeset_data(t)
    base = {"reverted": [], "conflict": [], "skipped": [], "dispatch_failed": [],
            "irreversible": (cs or {}).get("irreversible", [])}
    if cs is None or not t.project_id:
        return base
    ctx = _repo.get_or_load(t.project_id)
    reverted: list[str] = []
    dispatch_failed: list[dict] = []
    with ctx.lifecycle_guard():
        with ctx.lock:
            eff_actor = _require_edit_ctx(ctx, actor, request)
            planned = _plan_turn_undo_locked(ctx, cs.get("reversible", []))
            for w in planned["will_revert"]:
                item = next(p for p in planned["plan"] if p["op_id"] == w["op_id"])
                try:
                    args = _undo_compensation_args(ctx, item)

                    def _produce(_cmd=item["command_id"], _args=args, _a=eff_actor):
                        with credential_context(_a, _repo.credential_store,
                                                _repo.policy_store, _repo.tool_id):
                            return ctx.cmd_reg.dispatch(_cmd, _args, actor=_a, source="user",
                                                        doc=ctx.doc, op_log=None)

                    execution = ctx.commit.execute(_produce)
                    if execution.is_committed:
                        reverted.append(w["op_id"])
                    else:
                        dispatch_failed.append({"op_id": w["op_id"], "reason": "未提交"})
                except ZeError as e:                       # 单条补偿失败不阻断其余(尽力整轮撤销)
                    dispatch_failed.append({"op_id": w["op_id"], "reason": e.code})
            gen = int(ctx.generation)
    return {"reverted": reverted, "dispatch_failed": dispatch_failed,
            "conflict": planned["conflict"], "skipped": planned["skipped"],
            "irreversible": cs.get("irreversible", []), "project_generation": gen}


@app.post("/api/ai/turn/{turn_id}/cancel")
def ai_turn_cancel(turn_id: str, actor: Actor = Depends(_get_actor)):
    """§7.5 P0-E「中途取消」:用户显式取消当前这一轮。

    把 turn 收到 CANCELLED 终态并记一条 EV_TURN_END(outcome=cancelled)—— 轮的结局要留在事件流里,
    否则事后从 turn_id 追溯只看到「停在 acting」,分不清是取消还是崩了。cancel 可从任一非终态发生
    (状态机 §7.1);已是终态则**幂等返回**、不报错(重复点取消不该炸)。

    归属:非本人/不存在一律 entity_not_found(与 undo 端点同口径,不泄漏 turn 存在性)。

    **挂起确认一并释放(§7.4,2026-07-26 W1-5 落地)**:归属此前是空的 —— PendingConfirmation
    只有 trace_id 没有 turn_id,PendingStore 也没有枚举 API,所以本函数无从知道该释放哪些 token,
    这条要求挂了一个多月。用户 D1 拍板走方案 b(给 pending 内嵌 turn_id + 按轮枚举),而不是
    另建在飞轮次登记表 —— 后者在并发两轮时会按时间窗认错归属,把别人那轮的确认释放掉。
    现在:按 turn_id 找出本人仍 pending 的 token 逐个 cancel,释放数记进 turn_end 事件;
    reserved/frozen 不动(各有既有路径),如实列进 release_failed。
    """
    _require_tool_access(actor)
    ts = getattr(_repo, "turn_store", None)
    uid = actor.user_id
    t = ts.get_turn(turn_id) if (ts is not None and uid and not uid.startswith("__")) else None
    if t is None or t.actor_user_id != uid:
        raise ZeError("entity_not_found", "turn 不存在或非本人", context={})
    from sdk.assistant_turn import CANCELLED, EV_TURN_END, is_terminal
    if is_terminal(t.state):
        return {"cancelled": False, "state": t.state, "reason": "already_terminal"}
    # §7.4 P0-E(用户 D1 拍板方案 b,2026-07-26):**取消一轮必须把该轮挂起的确认一并释放**。
    # 留着悬挂 nonce 不是脏数据是安全问题:nonce 还活着就意味着那条高危命令仍可被批准执行,
    # 而用户以为这一轮已经取消。归属靠 PendingConfirmation.turn_id(内嵌,并发两轮也不会认错)。
    released, release_failed = 0, []
    if t.project_id:
        try:
            _ctx = _repo.get_or_load(t.project_id)
            _store = _ctx.cmd_reg.pending_store
            for _tok in _store.tokens_for_turn(turn_id, actor=actor):
                try:
                    if _store.cancel(_tok, actor=actor):
                        released += 1
                except ZeError as e:
                    # reserved(正在执行)/ frozen(需修复)不该被整轮取消顺手清掉 —— 它们各有
                    # 既有路径。如实报出,不静默吞:静默吞会让「取消了」这句话变成半真。
                    release_failed.append({"token": _tok, "code": e.code})
        except Exception:   # noqa: BLE001 — 释放尽力而为,不阻断取消本身
            pass
    # 幂等键让重放不重记结局事件(断线重试点两次取消 = 一条 turn_end)。
    ts.append_event(turn_id, EV_TURN_END,
                    {"outcome": "cancelled", "released_confirmations": released},
                    event_id=f"turnend:{turn_id}")
    updated = ts.transition(turn_id, CANCELLED)
    return {"cancelled": True, "state": updated.state,
            "released_confirmations": released, "release_failed": release_failed}


@app.get("/api/ai-consent")
def ai_consent(actor: Actor = Depends(_get_actor)):
    """AI 内容合规同意的条款数据源(对外公测门):返回部署配置的 {text, url, version}。
    version 为空 = 部署未配条款 → 前端 useAiConsent 判 accepted 恒真,门不激活(向后兼容,
    本地开发/未配 skill 零影响)。是否已接受由前端 localStorage 持有(条款可更新,version 变更
    即要求重签),此处只出条款文本,不做接受态存储。"""
    _require_tool_access(actor)
    return {
        "text": str(_repo.config.get("ze.ai.terms_text", "") or ""),
        "url": str(_repo.config.get("ze.ai.terms_url", "") or ""),
        "version": str(_repo.config.get("ze.ai.terms_version", "") or ""),
    }


@app.post("/api/account/credentials")
async def account_credentials_upsert(request: Request, actor: Actor = Depends(_get_actor)):
    """Create/update one binding for the current user. Secret is write-only."""
    _require_tool_access(actor)
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body must be JSON object")
    clean, body_key = _strip_body_idempotency_key(body)
    request_key = _request_idempotency_key(request, body_key)
    if not clean.get("id") and request_key:
        # Create 的业务 id 必须在 action 外稳定下来；否则 registry 已写、后续 CLI
        # auth 步骤失败后，同一幂等请求再次进入会随机生成第二个 credential。
        digest = hashlib.sha256(
            f"{actor.user_id}\0{request_key}".encode("utf-8")
        ).hexdigest()[:24]
        clean = {**clean, "id": f"cred_{digest}"}
    provider = str(clean.get("provider") or "")
    metadata = _credential_metadata_from_request(provider, clean.get("metadata"))
    clean = {**clean, "metadata": metadata}

    def _do() -> dict[str, Any]:
        # 绑定即验证(AI 通道层 3.56):gemini key 当场 models.list 最小校验(零生图消耗),
        # 坏 key 绑不进去 —— 不依赖测试账号,也不把失败推迟到第一次真用。
        if provider == PROVIDER_GEMINI_API and clean.get("secret") not in (None, ""):
            from sdk import gemini_validate_key
            gemini_validate_key(str(clean.get("secret")))
        # Cursor 只写本工具私有库,禁止进入 Hub credentials.json。
        store = _cursor_cred_mod().store_for_provider(provider, _repo.credential_store)
        rec = store.upsert_for_user(
            actor.user_id,
            credential_id=str(clean.get("id") or ""),
            provider=provider,
            label=str(clean.get("label") or ""),
            secret=(
                str(clean.get("secret"))
                if clean.get("secret") not in (None, "") else None
            ),
            metadata=metadata,
            capabilities=(
                clean.get("capabilities")
                if isinstance(clean.get("capabilities"), list) else None
            ),
        )
        # CLI home/auth 与 registry 尚是跨文件 workflow；各文件自身原子，W06
        # durable action journal 再把三步变成可恢复业务事务。
        if rec.provider in CLI_PROVIDERS:
            auth_file = (
                str(clean.get("auth_file") or "")
                if isinstance(clean.get("auth_file"), str) else ""
            )
            rec = _bind_cli_home(actor.user_id, rec, auth_file)
        return {"credential": _credential_public(rec)}

    return _run_idempotent(
        request,
        actor=actor,
        scope="account.credentials.upsert",
        body=clean,
        body_key=body_key,
        action=_do,
        outcome_unknown_codes=_CREDENTIAL_UNKNOWN_CODES,
    )


@app.post("/api/account/credentials/{credential_id}/default")
def account_credentials_set_default(
    credential_id: str,
    request: Request,
    actor: Actor = Depends(_get_actor),
):
    """Mark one binding as the user's default AI account; clears the flag on the rest.

    The floating copilot uses this to pick which account (and provider) drives
    `claude -p` / `codex exec` when the user has bound more than one."""
    _require_tool_access(actor)
    def _do() -> dict[str, Any]:
        store, _ = _cursor_cred_mod().resolve_store_for_credential(
            _repo.credential_store, actor.user_id, credential_id,
        )
        target = store.set_default_for_user(actor.user_id, credential_id)
        return {"ok": True, "credential": _credential_public(target)}

    return _run_idempotent(
        request,
        actor=actor,
        scope="account.credentials.set_default",
        body={"credential_id": credential_id},
        action=_do,
        outcome_unknown_codes=_CREDENTIAL_UNKNOWN_CODES,
    )


@app.post("/api/account/credentials/{credential_id}/verify")
def account_credentials_verify(
    credential_id: str,
    request: Request,
    actor: Actor = Depends(_get_actor),
):
    """Mark a credential as locally configured.

    This does not echo or test the secret against a vendor. Live provider checks belong in the
    first command that uses the credential, so errors can carry the exact provider response.
    """
    _require_tool_access(actor)
    def _do() -> dict[str, Any]:
        store, _ = _cursor_cred_mod().resolve_store_for_credential(
            _repo.credential_store, actor.user_id, credential_id,
        )
        rec = store.mark_verified(actor.user_id, credential_id)
        return {"ok": True, "credential": _credential_public(rec)}

    return _run_idempotent(
        request,
        actor=actor,
        scope="account.credentials.verify",
        body={"credential_id": credential_id},
        action=_do,
        outcome_unknown_codes=_CREDENTIAL_UNKNOWN_CODES,
    )


# 探活默认官方 base_url(与 _CREDENTIAL_ENDPOINT_HOSTS 官方 host 同源;gemini 无用户 base_url 恒用此)。
_PROBE_DEFAULT_BASE = {
    PROVIDER_OPENAI_API: "https://api.openai.com",
    PROVIDER_ANTHROPIC_API: "https://api.anthropic.com",
    PROVIDER_HUPO_GATEWAY: "https://api.ai.hupo.games",
    PROVIDER_GEMINI_API: "https://generativelanguage.googleapis.com",
}


def _guard_ai_outbound(provider: str, base_url: str) -> None:
    """P5:AI 探活/发现出站前 SSRF 护栏——官方 host 放行(零成本),用户自填 custom host 解析核验全公网
    (防 DNS rebinding)。base_url 已经 _credential_base_url 登记期护栏,这里补运行时解析核验。"""
    from sdk import hub  # noqa: E402
    host = (urllib.parse.urlsplit(base_url or "").hostname or "").lower()
    official = set(_CREDENTIAL_ENDPOINT_HOSTS.get(provider, frozenset()))
    dh = (urllib.parse.urlsplit(_PROBE_DEFAULT_BASE.get(provider, "")).hostname or "")
    if dh:
        official.add(dh.lower())
    try:
        hub.guard_outbound_host(host, official=official)
    except ValueError as e:
        raise ZeError("validation_failed", f"AI 端点被 SSRF 防护拒绝: {e}") from e


@app.post("/api/account/credentials/{credential_id}/probe")
def account_credentials_probe(
    credential_id: str,
    actor: Actor = Depends(_get_actor),
):
    """主动探活(ProviderHub 回收 P3b):对已登记凭据发最小 GET(/models 级)校验连通/鉴权。

    与 /verify 分工:verify 只标记本地已配(不打厂商,见其 docstring 既定契约);probe 是**兄弟端点**,
    真打厂商做一次性健康检查——不改 verify 契约、不落健康列(返 live 结果,避存量 store schema 加列雷)。
    reveal_secret 仅在**确认可探活后**于本次调用内注入鉴权、不落盘不回显(最小暴露);目标 host 由
    _credential_base_url 白名单校验(SSRF 唯一权威,当前仅官方 host);官方 CLI/内网服务无用户端点 →
    health=not_applicable(不解密)。非 mutation、不走 idempotent 缓存(每次都要 fresh 健康)。"""
    _require_tool_access(actor)
    from sdk import hub  # noqa: E402  (懒引受管 ProviderHub 内核;同 /api/ai-routes)
    store, rec = _cursor_cred_mod().resolve_store_for_credential(
        _repo.credential_store, actor.user_id, credential_id,
    )
    desc = hub.descriptor_for_provider(rec.provider)
    kind = desc.kind if desc else ""
    base_url = (_credential_base_url(rec.provider, rec.metadata.get("base_url"))
                or _PROBE_DEFAULT_BASE.get(rec.provider))
    if not base_url or not hub.is_probeable(kind):        # 官方登录/内网服务/未知 → 不解密
        return {"ok": True, "probe": {"health": "not_applicable",
                                      "detail": "该来源无用户可探活端点(官方登录或内网服务)"}}
    _guard_ai_outbound(rec.provider, base_url)            # P5:custom host 出站前解析核验(官方放行);失败即拒,不解密
    policy = (desc.endpoint_policy or {}) if desc else {}
    secret = store.reveal_secret(actor.user_id, credential_id)
    reqspec = hub.build_probe_request(kind, base_url, secret, test_path=policy.get("test_path", "/v1/models"))
    if reqspec is None:                                   # 双保险(kind 判定漂移)
        return {"ok": True, "probe": {"health": "not_applicable", "detail": "该来源不支持探活"}}
    url, headers = reqspec
    result = hub.probe_url(url, headers=headers)
    return {"ok": True, "probe": {"health": result.health, "error_code": result.error_code,
                                  "detail": result.detail, "latency_ms": result.latency_ms}}


@app.delete("/api/account/credentials/{credential_id}")
def account_credentials_delete(
    credential_id: str,
    request: Request,
    actor: Actor = Depends(_get_actor),
):
    _require_tool_access(actor)

    def _do() -> dict[str, Any]:
        store, _ = _cursor_cred_mod().resolve_store_for_credential(
            _repo.credential_store, actor.user_id, credential_id,
        )
        return {"deleted": store.delete_for_user(actor.user_id, credential_id)}

    return _run_idempotent(
        request,
        actor=actor,
        scope="account.credentials.delete",
        body={"credential_id": credential_id},
        action=_do,
        outcome_unknown_codes=_CREDENTIAL_UNKNOWN_CODES,
    )


# ==============================================================
# 设备/浏览器登录(傻瓜绑定 Codex / Claude):点按钮 → 浏览器登录 → 回来
# 后端跑 codex login --device-auth / claude setup-token,前端只显示授权链接 + 验证码。
# ==============================================================

def _cli_login_bins() -> dict:
    cfg = _repo.config
    return {"codex_bin": str(cfg.get("ze.llm.codex_bin", "codex")),
            "claude_bin": str(cfg.get("ze.llm.claude_bin", "claude"))}


@app.post("/api/account/cli-login/start")
async def cli_login_start(request: Request, actor: Actor = Depends(_get_actor)):
    """开始一次设备登录;返回授权链接(+ Codex 的验证码 / Claude 需回填 code)。"""
    _require_tool_access(actor)
    body = await request.json()
    provider = str((body or {}).get("provider") or "")
    # start 会阻塞至多 ~20s 等 CLI 打印授权链接 → 放线程,别占事件循环。
    return await asyncio.to_thread(
        LOGIN_MANAGER.start, actor.user_id, provider, **_cli_login_bins())


@app.post("/api/account/cli-login/poll")
async def cli_login_poll(request: Request, actor: Actor = Depends(_get_actor)):
    """轮询登录状态(Codex:等用户在浏览器批准 → 完成即建绑定)。"""
    _require_tool_access(actor)
    body = await request.json()
    login_id = str((body or {}).get("login_id") or "")
    return await asyncio.to_thread(
        LOGIN_MANAGER.poll, actor.user_id, login_id, _repo.credential_store)


@app.post("/api/account/cli-login/submit-code")
async def cli_login_submit_code(request: Request, actor: Actor = Depends(_get_actor)):
    """Claude:把网页给的授权 code 回填,完成绑定。"""
    _require_tool_access(actor)
    body = await request.json()
    login_id = str((body or {}).get("login_id") or "")
    code = str((body or {}).get("code") or "")
    return await asyncio.to_thread(
        LOGIN_MANAGER.submit_code, actor.user_id, login_id, code, _repo.credential_store)


@app.post("/api/account/cli-login/cancel")
def cli_login_cancel(login_id: str = Query(...), actor: Actor = Depends(_get_actor)):
    _require_tool_access(actor)
    return LOGIN_MANAGER.cancel(actor.user_id, login_id)


# ==============================================================
# 浮层 AI 协作面板(copilot):一轮 agentic 对话 = 一次 claude -p / codex exec
#
# 用户在浮层里发一句 → 本机 CLI agentic 操作本工具(经 loopback /mcp,source="ai")→
# 实体改动实时经 SSE 渲染 → 助手一句简洁回话回气泡。聊天气泡本身不入 Document(临时私有)。
# ==============================================================

_COPILOT_SYSTEM_BASE = (
    "你是嵌在 ZE 工具「{tool_id}」里的 AI 协作助手,和用户一起操作当前项目。\n"
    "你通过 ze 这组 MCP 工具读写本工具:先调 ze_describe 看对象图(实体类型/命令/编辑器),"
    "用 ze_entities 看现有实体,再用相应命令(如 entity.add / entity.update)完成用户的要求。"
    "你的每一步改动都会实时显示在用户的网页里。\n"
    "准则:干完一件事就简洁回一句结果;需要澄清就直接问;不要长篇解释。默认用中文回复。"
)


def _copilot_engine_for(actor: Actor) -> dict:
    """决定本轮 copilot 用哪个 agentic 引擎 + 每用户隔离 env。

    优先用户标记为默认的 CLI 绑定,否则第一条 active 的 claude/codex 绑定;
    都没有时,仅在 ze.ai.copilot_allow_ambient 开(本地开发)才用服务器本机登录兜底,
    否则 fail-closed(服务端多租户强制每人绑定,杜绝误用共享账号 + 署名错)。"""
    cfg = _repo.config
    rows = [r for r in _repo.credential_store.list_for_user(actor.user_id)
            if r.provider in CLI_PROVIDERS and r.status == "active"
            and r.metadata.get(META_HOME_DIR)]
    rows.sort(key=lambda r: (not bool(r.metadata.get(META_DEFAULT)),))   # 默认账号排前
    if rows:
        r = rows[0]
        engine = "claude" if r.provider == PROVIDER_CLAUDE_CLI else "codex"
        bin_key = "ze.llm.claude_bin" if engine == "claude" else "ze.llm.codex_bin"
        # Executable paths are deployment configuration, never user metadata.
        binary = str(cfg.get(bin_key, engine))
        return {"engine": engine, "binary": binary,
                "env": cli_home_env(r.provider, str(r.metadata.get(META_HOME_DIR))),
                "cred_id": r.id, "ambient": False}
    # 无 active CLI 绑定。ZE_SERVER_MODE(部署/多租户)硬门禁:强制 fail-closed,
    # 与 ze.ai.copilot_allow_ambient 无关 —— 多租户下 ambient 永不可能误用服务器共享登录。
    # 真值集含 "on"(与 sdk.model._truthy / mcp_bridge._TRUTHY / config KIND_BOOL 同口径),
    # 防裂脑:传输层已按宽集进 server 态时,此处不得因窄集漏判而 fail-open。
    server_mode = os.environ.get("ZE_SERVER_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    if server_mode or not bool(cfg.get("ze.ai.copilot_allow_ambient", True)):
        raise ZeError("requires_credential",
                      "请先在账号里绑定你自己的 Claude 或 Codex 账号(浮层 AI 用你的订阅额度)",
                      context={"capability": "agent"})
    # ambient 真兜底(仅本地开发,server_mode 关且 config 允许):留审计,便于事后发现配置漏关。
    _repo.auth_audit.append(
        "copilot_ambient_fallback",
        actor_user_id=actor.user_id,
        target_user_id=actor.user_id,
        tool_id=_repo.tool_id,
        source="ai",
    )
    engine = str(cfg.get("ze.ai.copilot_engine", "claude") or "claude")
    bin_key = "ze.llm.claude_bin" if engine == "claude" else "ze.llm.codex_bin"
    return {"engine": engine, "binary": str(cfg.get(bin_key, engine)),
            "env": {}, "cred_id": "", "ambient": True}


# Keywords that mark a CLI subprocess failure as an authentication problem (the
# stored OAuth blob no longer authenticates) rather than a transient/tool error.
# Kept conservative to avoid flipping a good binding on an unrelated failure.
_AUTH_FAILURE_MARKERS = (
    "unauthorized", "401", "authentication", "not logged in", "please log in",
    "please login", "log in again", "login expired", "session expired",
    "token expired", "invalid_grant", "invalid api key", "re-authenticate",
    "reauthenticate", "run \"claude", "run `claude", "setup-token", "oauth",
)


def _is_auth_failure(err: ZeError) -> bool:
    """True when a copilot subprocess failed because its login no longer works."""
    if err.code != "model_call_failed":
        return False
    text = f"{err.message} {err.context}".lower()
    return any(marker in text for marker in _AUTH_FAILURE_MARKERS)


def _self_mcp_url(
    project_id: str,
    request: Request | None = None,
    *,
    generation: int = 0,
    turn_id: str = "",
) -> str:
    """本机 loopback /mcp URL(供 agentic 子进程回调,单写不变量:仍经 server 改数据)。
    端口来源:① PORT env(start.sh export);② 请求到达的端口——直服 uvicorn(如 amber
    0.0.0.0:PORT 无反代)时,请求端口即监听端口,不依赖启动脚本导出 env。"""
    port = str(os.environ.get("PORT") or "").strip()
    if not port and request is not None:
        if request.url.port:
            port = str(request.url.port)
        else:
            host = request.headers.get("host", "")
            if ":" in host:
                port = host.rsplit(":", 1)[-1].strip()
    if not port:
        raise ZeError("model_call_failed",
                      "copilot 无法确定服务端口(PORT 未设且无法从请求推断)。", context={})
    query = urllib.parse.urlencode({
        "project": project_id,
        **({"generation": str(generation)} if generation > 0 else {}),
        # §7.4 P0-E:这一轮的 turn_id 随 URL 下发。子进程回调 /mcp 触发需确认命令时,
        # 产生的 PendingConfirmation 会带上它 —— 取消整轮才知道该释放哪些 nonce。
        **({"turn": turn_id} if turn_id else {}),
    })
    return f"http://127.0.0.1:{port}/mcp?{query}"


# 注入气泡的近期 oplog 窗口(只取结构信号:command_id/source/错误码/计数)。
_COPILOT_OPLOG_WINDOW = 30


def _copilot_intent_materials(ctx: Context, selection_input: Any) -> dict[str, Any]:
    """在 ctx.lock 下取一致快照:命令语义 + 选中实体结构摘要 + 近期 oplog + 活跃 task。

    选中实体的 data 读经本 ctx —— 该 ctx 已过 `_require_member` 项目级 ACL,与
    `ze_entities include_data` 同一道门,不新开旁路。此处只取**结构摘要**(type/id/name/
    字段名),完整值留给 AI 主动经 ze_entities include_data 拉(数据通道)。"""
    doc = ctx.doc
    selection: list[dict[str, Any]] = []
    for s in (selection_input or []):
        if not isinstance(s, dict):
            continue
        et, eid = str(s.get("type", "") or ""), str(s.get("id", "") or "")
        if not et or not eid:
            continue
        try:
            ent = doc.get(et, eid)
        except Exception:
            continue
        selection.append(structural_entity_summary(ent.to_dict()))
    oplog_entries = query_oplog(ctx.op_log, limit=_COPILOT_OPLOG_WINDOW).get("entries", [])
    return {
        "commands": list_commands(ctx.cmd_reg),
        "selection": selection,
        "oplog_entries": oplog_entries,
        "tasks": list_tasks(ctx.supervisor),
    }


_MAX_CHAT_IMAGES = 6
_MAX_CHAT_IMAGE_BYTES = 12 * 1024 * 1024
_CHAT_IMG_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def _stage_chat_images(raw: Any) -> tuple[list[str], str]:
    """把前端传的 data URI / {b64,mime} 图暂存成本地文件,返回 (路径列表, 暂存目录)。

    caller 用完删目录。限张数 + 单图大小;非图 / 超限 / 坏 base64 一律拒。暂存目录是新建的
    临时目录(runner 给 claude `--add-dir` 只读它 / codex `-i` 引用),不落进项目数据目录。"""
    items = raw if isinstance(raw, list) else []
    if not items:
        return [], ""
    if len(items) > _MAX_CHAT_IMAGES:
        raise ZeError("validation_failed", f"最多 {_MAX_CHAT_IMAGES} 张参考图", context={})
    tmpdir = tempfile.mkdtemp(prefix="ze-copilot-img-")
    paths: list[str] = []
    try:
        for i, it in enumerate(items):
            mime, b64 = "image/png", ""
            if isinstance(it, str) and it.startswith("data:"):
                head, _, data = it.partition(",")
                b64 = data
                m = re.match(r"data:([^;]+);base64", head)
                if m:
                    mime = m.group(1)
            elif isinstance(it, dict):
                b64 = str(it.get("b64") or it.get("data") or "")
                mime = str(it.get("mime") or "image/png")
            if not b64 or not mime.startswith("image/"):
                raise ZeError("validation_failed", "只接受图片附件", context={})
            try:
                blob = base64.b64decode(b64, validate=True)
            except Exception as e:
                raise ZeError("validation_failed", "图片 base64 解码失败", context={}) from e
            if len(blob) > _MAX_CHAT_IMAGE_BYTES:
                raise ZeError("validation_failed", "单张参考图超过大小上限(12MB)", context={})
            p = os.path.join(tmpdir, f"img{i}{_CHAT_IMG_EXT.get(mime, '.png')}")
            with open(p, "wb") as f:
                f.write(blob)
            paths.append(p)
        return paths, tmpdir
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def _chat_turn_start(actor: Actor, text: str, project_id: str = "") -> str:
    """开一轮助手 Turn + 记用户消息 + 推进 created→planning→acting(§7.1 P0)。**best-effort**:
    turn_store 缺 / 访客(空 uid / '__' 前缀)/ 任何异常 → 返 ''(不记、绝不阻断对话)。返 turn_id。
    project_id 记本轮操作的项目(P0-D 整轮撤销要 load 该项目 ctx)。"""
    ts = getattr(_repo, "turn_store", None)
    uid = actor.user_id
    if ts is None or not uid or uid.startswith("__"):
        return ""
    try:
        from sdk.assistant_turn import PLANNING, ACTING, EV_MESSAGE
        t = ts.create_turn(actor_user_id=uid, feature_id="assistant.chat", project_id=project_id)
        ts.append_event(t.turn_id, EV_MESSAGE, {"role": "user", "content": (text or "")[:500]})
        ts.transition(t.turn_id, PLANNING)
        ts.transition(t.turn_id, ACTING)
        return t.turn_id
    except Exception:   # noqa: BLE001 — Turn 记录 best-effort,绝不阻断对话
        return ""


def _chat_stage(ctx: Any, turn_id: str, stage: str, tool: str = "") -> None:
    """§7.3 W1-4:广播一轮内的阶段推进 + 记进 Turn 事件流。best-effort,绝不阻断对话。

    走既有 /api/events SSE(与 guide_step 同规矩,不新开通道)。为什么必须推而不是随响应回:
    /api/ai/chat 是一次 POST,结果回来时这一轮早结束了,那时再说「刚才在读取」没有意义 ——
    过程卡要在**过程中**动,五段里前四段全是过程。
    """
    if not turn_id:
        return
    try:
        from sdk.assistant_turn import EV_STAGE, stage_frame
        broker = getattr(ctx, "broker", None)
        if broker is not None:
            broker.publish_change(stage_frame(turn_id, stage, doc_id=ctx.doc.id, tool=tool))
        ts = getattr(_repo, "turn_store", None)
        if ts is not None:
            # 事件流里也留一份:事后从 turn_id 追溯时能看出卡在哪一段
            # (只有「完成」的话,失败轮永远说不清是读取炸了还是模型没回)。
            ts.append_event(turn_id, EV_STAGE, {"stage": stage, "tool": tool})
    except Exception:   # noqa: BLE001 — 呈现事件,坏了也不能影响对话
        pass


def _chat_turn_changeset(turn_id: str, op_log: Any, seq_before: int) -> None:
    """把本轮 agentic 跑产生的 source='ai' 实体改动聚合成一条 change_set 事件记进 Turn
    (§7.1 P0-D 整轮撤销的驱动数据:operation_ids 即本轮可逆单位)。best-effort,绝不阻断。

    窗口 = op_log 在本轮跑前后新增的条目(`entries()[seq_before:]`),过滤 source=='ai' 且
    无 error(失败 op 非可逆改动)。ctx.op_log 是同项目跨请求共享单例,loopback /mcp 的
    source='ai' op 写进的正是此实例,故本轮 agentic 的改动都落在此窗口。**归属精度**:AI op
    的 actor 统一是系统账号、无法按触发人区分,故按 wall-clock 窗口 + source 归属——常见
    「同项目同一时刻一轮」精确;同项目并发多人 AI 轮会交织,可能轻微过采(审计/撤销候选可接受)。
    幂等 event_id=`changeset:{turn_id}`:一轮至多记一条(成功/失败路径都调也只落一次)。"""
    ts = getattr(_repo, "turn_store", None)
    if ts is None or not turn_id or op_log is None:
        return None
    try:
        from sdk.assistant_turn import (EV_CHANGE_SET, changeset_from_oplog_rows,
                                        split_changeset_reversibility,
                                        changeset_entities_from_oplog_rows)
        rows = op_log.entries()[seq_before:]
        payload = changeset_from_oplog_rows(rows)
        if payload is None:
            return None
        # §7.4:同一窗口内把 operation_ids 拆可逆(纯本地实体改动)vs 不可逆外部效果(带 external
        # 信封的生成/外发),一并存进 change_set —— 整轮撤销预览「明确列出将撤销和不能撤销的部分」
        # 的数据源,P0-C 变更卡直接读。receipts 轮级 join 待 turnid-thread 接线,此刻 external_effects 略。
        split = split_changeset_reversibility(payload["operation_ids"], rows)
        payload["reversible"] = split["reversible"]
        payload["irreversible"] = split["irreversible"]
        # §7.3:抽本轮触及的实体+字段,变更卡「定位到实体/字段」+ /api/ai/turns 审计免回查 op_log。
        payload["entities"] = changeset_entities_from_oplog_rows(rows)
        ts.append_event(turn_id, EV_CHANGE_SET, payload, event_id=f"changeset:{turn_id}")
        return payload
    except Exception:   # noqa: BLE001 — change_set 记录 best-effort,不阻断对话
        return None


def _changeset_response_summary(payload: "dict | None") -> "dict | None":
    """把 change_set payload 压成给 /api/ai/chat 响应的紧凑摘要(§7.3 变更卡免额外往返直接渲染)。
    None/空 → None(纯回答轮无此字段)。只出计数 + 实体 + 可撤性,不带完整 operation_ids(那走 /api/ai/turns)。"""
    if not payload or not payload.get("operation_ids"):
        return None
    return {"count": payload.get("count", 0),
            "reversible_count": len(payload.get("reversible", [])),
            "irreversible_count": len(payload.get("irreversible", [])),
            "entities": payload.get("entities", [])}


def _chat_turn_end(turn_id: str, *, ok: bool, reply: str = "") -> None:
    """收一轮:记助手回话(成功且非空)+ 推进 completed/failed(§7.1)。best-effort,同上不阻断。"""
    ts = getattr(_repo, "turn_store", None)
    if ts is None or not turn_id:
        return
    try:
        from sdk.assistant_turn import COMPLETED, FAILED, EV_MESSAGE
        if ok and reply:
            ts.append_event(turn_id, EV_MESSAGE, {"role": "assistant", "content": reply[:500]})
        ts.transition(turn_id, COMPLETED if ok else FAILED)
    except Exception:   # noqa: BLE001
        pass


@app.post("/api/ai/chat")
async def ai_chat(
    request: Request,
    project_id: str = Query(...),
    ctx: Context = Depends(_require_member),
    actor: Actor = Depends(_get_actor),
):
    """一轮浮层 AI 对话。同步返回助手回话;工具改动实时经 /api/events SSE 渲染。

    并发/死锁安全:agentic 子进程经 `asyncio.to_thread` 跑(不占事件循环、不持 ctx.lock);
    子进程的 MCP 回调由本 server 另开线程各自短暂持锁处理(单写者仍是 server)。"""
    cfg = _repo.config
    if not bool(cfg.get("ze.ai.copilot_enabled", True)):
        raise ZeError("validation_failed", "浮层 AI 未启用", context={})
    _require_tool_access(actor)
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body must be JSON object")
    expected_generation = _request_expected_generation(request, body)
    selection_input = body.get("selection") if isinstance(body.get("selection"), list) else []
    focus_editor = str(body.get("focus_editor") or body.get("focusEditor") or "")
    with ctx.lifecycle_guard():
        _require_context_generation(ctx, expected_generation)
        # This is the authority snapshot used to mint the callback token.  ACL
        # checks outside ``ctx.lock`` are only advisory: an owner may revoke the
        # caller while this request is queued for the document writer lock.
        with ctx.lock:
            _require_edit_ctx(ctx, actor, request)
            project_generation = int(ctx.generation)
            # 同一持锁快照取 IntentContext 取料(过本 ctx 的项目 ACL)。
            materials = _copilot_intent_materials(ctx, selection_input)
    text = str(body.get("text") or "").strip()
    image_paths, img_dir = _stage_chat_images(body.get("images"))
    if not text and not image_paths:
        raise ZeError("validation_failed", "消息不能为空", context={})
    eng = _copilot_engine_for(actor)
    # 两通道组装:system(工具是什么 + 工作方式,指令通道)/ data_context(此刻语境,数据通道)。
    composed = compose_copilot_context(
        base_system=_COPILOT_SYSTEM_BASE.format(tool_id=_repo.tool_id),
        tool_ctx={
            "tool_id": _repo.tool_id,
            "ai_flow": _repo.introspect.ai_flow,
            "manual": _repo.introspect.manual,
            "commands": materials["commands"],
            # U0 实例(有则 static_tool_layer 从它派生权威静态层;无则回退 ai_flow+说明书)
            "tool_understanding": _repo.introspect.tool_understanding,
        },
        selection=materials["selection"],
        oplog_entries=materials["oplog_entries"],
        tasks=materials["tasks"],
        focus_editor=focus_editor,
    )
    system = composed["system"]
    prompt = fold_data_channel(composed["data_context"], text)
    timeout = int(cfg.get("ze.ai.copilot_timeout_sec", 300) or 300)
    # §7.1 记一轮 Turn(best-effort,不阻断)。**必须先于 mcp_url**:turn_id 要随 URL
    # 传给子进程,它回调 /mcp 时才能把该轮产生的 pending 归到这一轮
    # (§7.4 P0-E 按轮释放的前提;不传 = tokens_for_turn 永远空 = 释放是摆设)。
    _turn = _chat_turn_start(actor, text, project_id)
    mcp_url = _self_mcp_url(project_id, request, generation=project_generation, turn_id=_turn)
    # 为已认证用户铸临时单次 token(TTL=超时+余量),子进程用它 AS 该用户回调 /mcp;完成即撤。
    token = _repo.session_mgr.mint_session(actor.user_id, ttl_sec=timeout + 30)
    _chat_stage(ctx, _turn, "understanding")   # §7.3 W1-4 ①收到这一轮
    _chat_stage(ctx, _turn, "reading")         #        ②语境已装配(compose_copilot_context 在上面)
    _chat_stage(ctx, _turn, "waiting")         #        ③等模型;工具调用阶段由 MCP 侧打点
    _seq_before = len(ctx.op_log)           # §7.1 P0-D:卡 OpLog 游标,跑后取本轮 source='ai' 改动
    try:
        result = await asyncio.to_thread(
            agent_chat, engine=eng["engine"], binary=eng["binary"], prompt=prompt,
            mcp_url=mcp_url, mcp_token=token, system=system, env=eng["env"], timeout=timeout,
            image_paths=image_paths,
        )
    except ZeError as e:
        # The subprocess ran under a real per-user CLI binding (not ambient) and
        # failed to authenticate: flip that binding to needs_reauth so later turns
        # skip it (→ requires_credential) and the account panel offers re-login.
        if eng["cred_id"] and _is_auth_failure(e):
            _repo.credential_store.mark_needs_reauth(actor.user_id, eng["cred_id"])
        _chat_turn_changeset(_turn, ctx.op_log, _seq_before)   # §7.1 P0-D:失败前的部分改动也聚合(可逆)
        _chat_turn_end(_turn, ok=False)     # §7.1 失败收一轮
        raise
    finally:
        _repo.session_mgr.logout(token)     # 撤销临时 token
        if img_dir:
            shutil.rmtree(img_dir, ignore_errors=True)   # 清临时参考图
    _cs = _chat_turn_changeset(_turn, ctx.op_log, _seq_before)   # §7.1 P0-D:本轮 AI 改动聚合成 change_set
    _chat_stage(ctx, _turn, "done")                                 # §7.3 W1-4 ⑤收束
    _chat_turn_end(_turn, ok=True, reply=result.get("reply", ""))   # §7.1 成功收一轮
    return {
        "reply": result.get("reply", ""),
        "engine": result.get("engine", eng["engine"]),
        "ambient": eng["ambient"],
        "project_generation": project_generation,
        "turn_id": _turn,
        # §7.3 变更卡免额外往返:紧凑摘要(计数 + 实体 + 可撤性);纯回答轮为 None。整轮撤销走 turn_id。
        "change_set": _changeset_response_summary(_cs),
    }


# ==============================================================
# 模型权限 / 飞书后台策略 REST
# ==============================================================

def _model_policy_payload(actor: Actor) -> dict[str, Any]:
    user = _require_tool_access(actor)
    rows = _repo.credential_store.list_for_user(actor.user_id)
    hupo_llm = [
        _credential_public(r)
        for r in rows
        if r.provider == PROVIDER_HUPO_GATEWAY
        and "llm" in getattr(r, "capabilities", ())
        and getattr(r, "status", "") == "active"
    ]
    return {
        "tool_id": _repo.tool_id,
        "permission": _effective_permission(user),
        "model_scopes": {
            scope: _repo.policy_store.get_model_scope_permission(user.user_id, _repo.tool_id, scope)
            for scope in MODEL_SCOPES
        },
        "credentials": {
            PROVIDER_HUPO_GATEWAY: {
                "configured": any(r.get("has_secret") for r in hupo_llm),
                "credentials": hupo_llm,
            },
        },
    }


def _policy_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "allow", "allowed", "enable", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "deny", "denied", "disable", "disabled"}:
        return False
    raise ZeError("validation_failed", "model_scopes values must be booleans")


@app.get("/api/account/model-policy")
def account_model_policy(actor: Actor = Depends(_get_actor)):
    """Return current user's effective tool/model permissions and LLM credential readiness."""
    return _model_policy_payload(actor)


@app.post("/api/account/permission-request")
async def account_permission_request(request: Request, actor: Actor = Depends(_get_actor)):
    """Record a user's request for edit access.

    这是自助入口,不是自助提权:显式 read/ban 策略仍由管理员/Hub 决定。没有显式策略的登录
    用户会被 _effective_permission 视为 edit,因此这里会返回 already_granted。

    **只校登录,不校访问权**:本端点存在的理由就是让**没有访问权的人**申请访问权。
    用 _require_tool_access 把门的话,none 档用户会被它挡在外面 —— 而挡他的那句提示
    正是「可在工具内提交申请」,自相矛盾,申请永远递不进队列。
    """
    _require_login(actor)
    user = _repo.user_store.get(actor.user_id)
    if user is None:
        raise ZeError("auth_required", "login required")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    current = _effective_permission(user)
    requested = str(body.get("permission") or PERMISSION_EDIT).strip() or PERMISSION_EDIT
    if current == PERMISSION_EDIT:
        return {
            "ok": True,
            "status": "already_granted",
            "tool_id": _repo.tool_id,
            "current_permission": current,
        }
    if requested not in {PERMISSION_EDIT, PERMISSION_READ}:
        requested = PERMISSION_EDIT
    # 落进 PolicyStore 的申请队列 —— 审计日志只能事后翻,管理台需要能列出「待办」。
    # 队列以 用户×工具 为唯一键,重复提交更新同一行。
    try:
        _repo.policy_store.request_access(
            user.user_id, _repo.tool_id,
            requested=requested, reason=str(body.get("reason") or ""),
        )
    except ZeError:
        # 已有更高权限 / 已被封禁 —— 审计仍照记,不因队列拒绝而丢掉这次意图。
        pass
    _repo.auth_audit.append(
        "tool_permission_requested",
        actor_user_id=user.user_id,
        target_user_id=user.user_id,
        tool_id=_repo.tool_id,
        source="user",
        current_permission=current,
        requested_permission=requested,
        reason=str(body.get("reason") or ""),
    )
    return {
        "ok": True,
        "status": "recorded",
        "tool_id": _repo.tool_id,
        "current_permission": current,
        "requested_permission": requested,
    }


@app.get("/api/admin/policies")
def admin_policies(actor: Actor = Depends(_get_actor)):
    """Read raw ZE policy table. Intended for Feishu admin surfaces."""
    _require_super_admin(actor)
    return _repo.policy_store.all_policies()


@app.post("/api/admin/policies")
async def admin_policy_update(request: Request, actor: Actor = Depends(_get_actor)):
    """Update one user's tool permission and/or per-model-scope switches."""
    admin = _require_super_admin(actor)
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body must be JSON object")
    user_id = str(body.get("user_id") or "").strip()
    tool_id = str(body.get("tool_id") or _repo.tool_id).strip()
    if not user_id:
        raise ZeError("validation_failed", "user_id is required")
    if not tool_id:
        raise ZeError("validation_failed", "tool_id is required")

    changed: dict[str, Any] = {}
    if body.get("permission") is not None:
        want = str(body.get("permission"))
        if want.strip().lower() in {"", "inherit"}:
            # 撤回显式授权 → 回到跟随工具默认档。没有这条路的话授权是单向门:
            # 一旦给过就再也回不到「跟随」,之后改默认档对这个人不再生效。
            _repo.policy_store.clear_permission(user_id, tool_id)
            changed["permission"] = "inherit"
        else:
            changed["permission"] = _repo.policy_store.set_permission(user_id, tool_id, want)
    scopes = body.get("model_scopes", body.get("scopes", {}))
    if scopes is None:
        scopes = {}
    if not isinstance(scopes, dict):
        raise ZeError("validation_failed", "model_scopes must be an object")
    changed_scopes: dict[str, bool] = {}
    for scope, enabled in scopes.items():
        changed_scopes[str(scope)] = _repo.policy_store.set_model_scope(
            user_id,
            tool_id,
            str(scope),
            _policy_bool(enabled),
        )
    if changed_scopes:
        changed["model_scopes"] = changed_scopes
    if not changed:
        raise ZeError("validation_failed", "permission or model_scopes is required")

    _repo.auth_audit.append(
        "tool_policy_changed",
        actor_user_id=admin.user_id,
        target_user_id=user_id,
        tool_id=tool_id,
        source="admin_api",
        **changed,
    )
    policies = _repo.policy_store.all_policies()
    return {
        "ok": True,
        "user_id": user_id,
        "tool_id": tool_id,
        "policy": (policies.get("users") or {}).get(user_id, {}),
    }


@app.get("/api/admin/access-requests")
def admin_access_requests(state: str = Query(default="pending"), actor: Actor = Depends(_get_actor)):
    """待审批的权限申请队列。state 传空串则列全部。"""
    _require_super_admin(actor)
    rows = _repo.policy_store.list_access_requests(state)
    # 把申请人的可读身份一并带上:管理台列表不该只有一串 user_id。
    out = []
    for row in rows:
        u = _repo.user_store.get(row.get("user_id") or "")
        out.append({
            **row,
            "display_name": getattr(u, "display_name", "") if u else "",
            "email": getattr(u, "email", "") if u else "",
        })
    return {"ok": True, "requests": out, "state": state}


@app.post("/api/admin/access-requests/resolve")
async def admin_resolve_access_request(request: Request, actor: Actor = Depends(_get_actor)):
    """批准 / 驳回一条申请。批准即授权(不留「批了但没生效」的中间态)。"""
    admin = _require_super_admin(actor)
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body must be JSON object")
    user_id = str(body.get("user_id") or "").strip()
    tool_id = str(body.get("tool_id") or _repo.tool_id).strip()
    approve = _policy_bool(body.get("approve", True))
    row = _repo.policy_store.resolve_access_request(
        user_id, tool_id, approve=approve, decided_by=admin.user_id)
    _repo.auth_audit.append(
        "tool_permission_resolved",
        actor_user_id=admin.user_id, target_user_id=user_id, tool_id=tool_id,
        source="admin_api", state=row.get("state"), granted=row.get("requested") if approve else "",
    )
    return {"ok": True, "request": row}


@app.post("/api/admin/tool-default")
async def admin_set_tool_default(request: Request, actor: Actor = Depends(_get_actor)):
    """设某工具的默认档 —— 标准 skill 设 none 即「默认无权限,须申请」。"""
    admin = _require_super_admin(actor)
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body must be JSON object")
    tool_id = str(body.get("tool_id") or _repo.tool_id).strip()
    permission = str(body.get("permission") or PERMISSION_NONE).strip()
    applied = _repo.policy_store.set_tool_default(tool_id, permission)
    _repo.auth_audit.append(
        "tool_default_changed",
        actor_user_id=admin.user_id, target_user_id="", tool_id=tool_id,
        source="admin_api", permission=applied,
    )
    return {"ok": True, "tool_id": tool_id, "permission": applied,
            "tool_defaults": _repo.policy_store.tool_defaults()}


@app.get("/api/admin/users")
def admin_search_users(
    q: str = Query(default=""),
    limit: int = Query(default=50),
    tool_id: str = Query(default=""),
    actor: Actor = Depends(_get_actor),
):
    """按名称/邮箱/ID 检索用户,并带上其在指定工具上的当前权限。

    用户很多,管理台按工具管权限时必须能搜 —— 全量列表不可用。
    """
    _require_super_admin(actor)
    target_tool = (tool_id or _repo.tool_id).strip()
    users = search_users(_repo.user_store.list_users(), query=q, limit=limit,
                         include_disabled=True)
    explicit = {}
    for u in users:
        explicit[u.user_id] = _repo.policy_store.user_permissions(u.user_id).get(target_tool, "")
    return {
        "ok": True,
        "tool_id": target_tool,
        "tool_default": _repo.policy_store.tool_default(target_tool),
        "users": [
            {
                "user_id": u.user_id,
                "display_name": u.display_name,
                "email": u.email,
                "status": u.status,
                # 空 = 没有显式授权(落工具默认档),与显式设成 read 是两回事
                "explicit_permission": explicit.get(u.user_id, ""),
                "effective_permission": _repo.policy_store.get_permission(u.user_id, target_tool),
            }
            for u in users
        ],
    }


# ==============================================================
# 配置中枢 REST(skill 级;经 04-ops ConfigRegistry)
# ==============================================================

@app.get("/api/config")
def get_config(actor: Actor = Depends(_get_actor)):
    """读全部配置项 + 生效值(secret 一律脱敏 ***)。要登录(不向匿名泄露端点/掩码项)。

    skill 级配置(非 per-project),故不走 _require_member;登录即可读。
    """
    _require_tool_access(actor)
    return {
        "config": _repo.config.snapshot(),
        "version": _repo.config.version,
    }


@app.put("/api/config")
async def put_config(request: Request, actor: Actor = Depends(_get_actor)):
    """全局配置写入口：仅 ZE super-admin 可调用，expected_version 必填且为非负整数。

    secret 类只存不回显明文，绝不进 git/日志。项目 owner/edit 权限不等于全局配置权限；
    只读项（框架/端口）仍由 ConfigRegistry 拒绝覆盖。
    """
    _require_super_admin(actor)
    body = await request.json()
    updates = body.get("updates") if isinstance(body, dict) else None
    if not isinstance(updates, dict):
        raise ZeError("validation_failed", "body 需含 updates: {key: value}")
    expected_version = body.get("expected_version") if isinstance(body, dict) else None
    if (
        not isinstance(expected_version, int) or isinstance(expected_version, bool)
        or expected_version < 0
    ):
        raise ZeError("validation_failed", "expected_version 是必填的非负整数")
    clean, body_key = _strip_body_idempotency_key(body)

    def _do() -> dict[str, Any]:
        rejected = _repo.save_config(updates, expected_version=expected_version)
        return {
            "rejected": [{"key": k, "reason": r} for k, r in rejected],
            "config": _repo.config.snapshot(),
            "version": _repo.config.version,
        }

    return _run_idempotent(
        request,
        actor=actor,
        scope="config.put",
        body=clean,
        body_key=body_key,
        action=_do,
        # replace 后目录 fsync 失败意味着配置可能已耐久；同 key 自动重放会
        # 把“结果未知”误当成“没写”。保留 repair tombstone，直到 W06 事务账本对账。
        outcome_unknown_codes=("config_durability_unknown",),
    )


# ==============================================================
# 项目 REST
# ==============================================================

@app.get("/api/projects")
def list_projects(response: Response, actor: Actor = Depends(_get_actor)):
    """列当前用户可见的项目(owner 自己 / 被加为 collaborator / 无主公共)。"""
    _require_tool_access(actor)
    response.headers["Cache-Control"] = "no-store"
    out = []
    seen_ids: set[str] = set()
    for m in _repo.list_project_metas():
        pid = m["id"]
        if pid in seen_ids:
            raise ZeError(
                "project_recovery_required", "project catalog contains duplicate ids",
                context={"project_id": pid},
            )
        seen_ids.add(pid)
        acl = m["acl"]
        # demo 沙盒(隐藏项目)不进普通项目列表 —— 只能从「示例」入口打开/重置。
        if (m.get("config") or {}).get("project_kind") == "demo_session":
            continue
        if _can_access(acl, actor.user_id):
            out.append({
                "id": pid, "name": m["name"],
                "owner": acl.get("owner", ""),
                "collaborators": acl.get("collaborators") or [],
                "generation": m["generation"],
                "updated_at": m["updated_at"],
                "is_owner": actor.user_id == acl.get("owner", ""),
                "git_linked": bool(m.get("git_linked")),
            })
    out.sort(key=lambda p: p["updated_at"], reverse=True)
    return {"projects": out}


# ---- 项目级 git 关联(R-C17):owner/repo/ref 属项目;PAT 留账号级 credential ----

def _require_project_member(project_id: str, actor: Actor, *, edit: bool) -> None:
    """git-link 路由的 ACL 门:登录 + 工具权限 + 项目成员(edit 时另须编辑权)。"""
    if edit:
        _require_edit(actor)
    else:
        _require_tool_access(actor)
    meta = _repo.project_meta(project_id)
    if not _can_access(meta["acl"], actor.user_id):
        raise ZeError("acl_denied", "当前用户不是该项目成员",
                      context={"project_id": project_id})


@app.get("/api/projects/{project_id}/git-link")
def project_git_link_get(
    project_id: str, response: Response, actor: Actor = Depends(_get_actor),
) -> dict:
    """本项目关联的 git 仓库(owner/repo/ref);未关联 → git_link: null。"""
    _require_project_member(project_id, actor, edit=False)
    response.headers["Cache-Control"] = "no-store"
    return {"git_link": _repo.read_project_git_link(project_id)}


@app.put("/api/projects/{project_id}/git-link")
async def project_git_link_put(
    project_id: str, request: Request, actor: Actor = Depends(_get_actor),
) -> dict:
    """设置本项目 git 关联(owner/repo 必填, all-or-nothing;ref 可选)。"""
    _require_project_member(project_id, actor, edit=True)
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body 必须是 JSON 对象")
    owner = github_validate_owner(body.get("owner"))
    repo_name = github_validate_repo(body.get("repo"))
    ref_value = body.get("ref")
    ref = github_validate_ref(ref_value) if ref_value else ""

    def _action() -> dict:
        return {"git_link": _repo.set_project_git_link(
            project_id, owner=owner, repo=repo_name, ref=ref,
            actor_user_id=actor.user_id)}

    return _run_idempotent(
        request, actor=actor, scope="project.git_link.put",
        project_id=project_id, body=body, action=_action,
    )


@app.delete("/api/projects/{project_id}/git-link")
def project_git_link_delete(
    project_id: str, request: Request, actor: Actor = Depends(_get_actor),
) -> dict:
    """解除本项目 git 关联(不动账号级 PAT)。"""
    _require_project_member(project_id, actor, edit=True)

    def _action() -> dict:
        return {"deleted": _repo.clear_project_git_link(project_id)}

    return _run_idempotent(
        request, actor=actor, scope="project.git_link.delete",
        project_id=project_id, action=_action,
    )


@app.post("/api/projects")
async def create_project(request: Request, actor: Actor = Depends(_get_actor)):
    _require_edit(actor)
    body = await request.json() if request.headers.get("content-length") else {}
    if not isinstance(body, dict):
        body = {}
    clean, body_key = _strip_body_idempotency_key(body)
    intent_id, _ = _require_topology_request(request, clean, body_key=body_key)
    target_id = clean.get("project_id")
    if not isinstance(target_id, str) or not target_id:
        raise ZeError("validation_failed", "project_id is required")

    def _do() -> dict[str, Any]:
        commit = _repo.create_project(
            name=clean.get("name") or "未命名项目",
            owner=actor.user_id,
            seed=bool(clean.get("seed")),
            project_id=target_id,
            intent_id=intent_id,
            return_commit=True,
        )
        return {
            "project_id": commit.project_id,
            "intent_id": intent_id,
            "status": "committed",
            "mutation_outcome": "committed",
            "kind": "create",
            "generation": commit.generation,
            "project_generation": commit.generation,
        }

    return _run_topology_idempotent(
        request, actor=actor, scope="projects.create",
        project_id=target_id, body=clean, body_key=body_key, action=_do,
        intent_id=intent_id, kind="create", target_id=target_id,
    )


@app.post("/api/projects/{project_id}/duplicate")
async def duplicate_project(project_id: str, request: Request,
                            actor: Actor = Depends(_get_actor)):
    """复制项目(header-v2 §5.10):普通项目要写权;demo 另存只要求登录且可访问源沙盒。"""
    user = _require_tool_access(actor)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    clean, body_key = _strip_body_idempotency_key(body)
    expected_generation = _request_expected_generation(request, clean)
    clean = {
        **_without_expected_generation(clean),
        "expected_generation": expected_generation,
    }
    intent_id, _ = _require_topology_request(request, clean, body_key=body_key)
    new_id = clean.get("project_id")
    if not isinstance(new_id, str) or not new_id:
        raise ZeError("validation_failed", "project_id is required")

    def _verify_source(root: Path) -> None:
        metadata = read_document_metadata(root / "document.json")
        if metadata.id != project_id:
            raise ZeError(
                "project_recovery_required",
                "source project identity is invalid",
                context={"project_id": project_id, "reason": "document_id_mismatch"},
            )
        acl = validate_document_acl(
            metadata.acl,
            error_code="project_recovery_required",
            context={"project_id": project_id},
        )
        config = metadata.config
        if not _can_access(acl, actor.user_id):
            raise ZeError("acl_denied", "无权访问该项目", context={"project_id": project_id})
        if config.get("project_kind") == "demo_session" and acl.get("owner") != actor.user_id:
            raise ZeError("acl_denied", "只能另存自己的示例沙盒")

    def _do() -> dict[str, Any]:
        meta = _repo.project_meta(project_id)
        if meta["generation"] != expected_generation:
            raise ZeError(
                "project_generation_stale", "source project changed before duplicate",
                context={
                    "project_id": project_id,
                    "expected_generation": expected_generation,
                    "current_generation": meta["generation"],
                },
            )
        is_demo = (meta.get("config") or {}).get("project_kind") == "demo_session"
        if not _can_access(meta["acl"], actor.user_id):
            raise ZeError("acl_denied", "无权访问该项目", context={"project_id": project_id})
        if is_demo and meta["acl"].get("owner") != actor.user_id:
            raise ZeError("acl_denied", "只能另存自己的示例沙盒")
        if not is_demo and _effective_permission(user) != PERMISSION_EDIT:
            raise ZeError("read_only_violation", "当前用户是只读权限,不能执行写操作")
        # Topology holds lifecycle EX plus the source Document's content SH
        # from this verifier through safe copy and target publication, so a
        # same-generation ACL edit cannot win a TOCTOU.
        name = clean.get("name")
        commit = _repo.duplicate_project(
            project_id, owner=actor.user_id,
            name=str(name) if name else None,
            project_id=new_id,
            intent_id=intent_id,
            expected_generation=expected_generation,
            verify_source=_verify_source,
            return_commit=True,
        )
        return {
            "project_id": commit.project_id,
            "source_project_id": project_id,
            "source_generation": expected_generation,
            "intent_id": intent_id,
            "status": "committed",
            "mutation_outcome": "committed",
            "kind": "duplicate",
            "generation": commit.generation,
            "project_generation": commit.generation,
        }

    return _run_topology_idempotent(
        request, actor=actor, scope="projects.duplicate",
        project_id=project_id, body=clean, body_key=body_key, action=_do,
        intent_id=intent_id, kind="duplicate", target_id=new_id,
    )


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, request: Request,
                         actor: Actor = Depends(_get_actor)):
    """删除项目(header-v2 §5.9)。双保险服务端硬校验:
    owner-only + confirm_name 必须精确等于项目名(红线不只靠前端 dialog)。"""
    _require_edit(actor)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    clean, body_key = _strip_body_idempotency_key(body)
    expected_generation = _request_expected_generation(request, clean)
    clean = {
        **_without_expected_generation(clean),
        "expected_generation": expected_generation,
    }
    intent_id, _ = _require_topology_request(request, clean, body_key=body_key)

    def _verify_existing(root: Path) -> None:
        metadata = read_document_metadata(root / "document.json")
        if metadata.id != project_id:
            raise ZeError(
                "project_recovery_required",
                "project identity is invalid",
                context={"project_id": project_id, "reason": "document_id_mismatch"},
            )
        acl = validate_document_acl(
            metadata.acl,
            error_code="project_recovery_required",
            context={"project_id": project_id},
        )
        if acl.get("owner", "") != actor.user_id:
            raise ZeError("acl_denied", "只有项目 owner 可以删除项目")
        if str(clean.get("confirm_name", "")) != str(metadata.name or project_id):
            raise ZeError("validation_failed", "confirm_name 与项目名不一致")

    def _do() -> dict[str, Any]:
        # Keep every pre-cutover check inside the evidence-aware wrapper.  A
        # retry after a proven delete may see project_not_found/stale, which is
        # not evidence that the original intent never committed.
        try:
            meta = _repo.project_meta(project_id)
        except ZeError as exc:
            if exc.code != "project_not_found":
                raise
            meta = None
        if meta is not None:
            if meta["generation"] != expected_generation:
                raise ZeError(
                    "project_generation_stale", "project changed before delete",
                    context={
                        "project_id": project_id,
                        "expected_generation": expected_generation,
                        "current_generation": meta["generation"],
                    },
                )
            if meta["acl"].get("owner", "") != actor.user_id:
                raise ZeError("acl_denied", "只有项目 owner 可以删除项目",
                              context={"project_id": project_id})
            if str(clean.get("confirm_name", "")) != meta["name"]:
                raise ZeError(
                    "validation_failed",
                    "confirm_name 与项目名不一致:删除必须手打完整项目名确认",
                    context={"project_id": project_id},
                )
        commit = _repo.delete_project(
            project_id,
            intent_id=intent_id,
            expected_generation=expected_generation,
            verify_existing=_verify_existing,
            principal_id=actor.user_id,
            return_commit=True,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "intent_id": intent_id,
            "status": "deleted",
            "mutation_outcome": "committed",
            "kind": "delete",
            "generation": commit.generation,
            "project_generation": commit.generation,
        }

    return _run_topology_idempotent(
        request, actor=actor, scope="projects.delete",
        project_id=project_id, body=clean, body_key=body_key, action=_do,
        intent_id=intent_id, kind="delete", target_id=project_id,
    )


@app.get("/api/projects/{project_id}/intents/{intent_id}")
def project_intent_status(project_id: str, intent_id: str,
                          response: Response,
                          kind: str = Query(...),
                          actor: Actor = Depends(_get_actor)):
    """Reconcile a lost create/duplicate/delete response without replaying it.

    A deleted target no longer has ACL metadata, so the random target+intent
    pair acts as the reconciliation capability.  Live targets still enforce
    the normal project ACL before returning their proof.
    """
    _require_tool_access(actor)
    response.headers["Cache-Control"] = "no-store"
    if kind not in ("create", "duplicate", "delete"):
        raise ZeError("validation_failed", "invalid public project operation kind")
    # A live target always retains its normal ACL regardless of whether the
    # proof says committed/conflict/not_committed. Deleted targets have no ACL
    # row, so their random target+intent pair remains the reconciliation token.
    try:
        meta = _repo.project_meta(project_id)
    except ZeError as exc:
        if exc.code != "project_not_found":
            raise
    else:
        if not _can_access(meta["acl"], actor.user_id):
            raise ZeError(
                "acl_denied",
                "无权查看该项目操作状态",
                context={"project_id": project_id},
            )
    status = _repo.project_intent_status(
        project_id, intent_id, kind=kind, principal_id=actor.user_id,
    )
    # Proof wire v2 is deliberately self-contained.  In particular,
    # ``intent_reserved`` distinguishes a pure point-in-time negative read from
    # an already claimed intent; the old mutation_outcome alias collapsed those
    # states and is no longer emitted by reconciliation endpoints.
    return status


# ==============================================================
# demo 模板(一等文件系统能力:多模板 catalog + 隐藏可重置沙盒)
# ==============================================================

@app.get("/api/demo-templates")
def list_demo_templates(response: Response, request: Request,
                        actor: Actor = Depends(_get_actor)):
    """列出 mod 声明的示例场景(元数据;不含 bootstrap 函数引用)。免登录可见 —— demo 人人可开。"""
    response.headers["Cache-Control"] = "no-store"
    owner_id = actor.user_id
    result: dict[str, Any] = {}
    if not owner_id:
        owner_id = request.cookies.get(GUEST_COOKIE_NAME) or ""
    if not owner_id:
        owner_id = f"g_{secrets.token_hex(12)}"
        response.set_cookie(
            GUEST_COOKIE_NAME,
            owner_id,
            max_age=GUEST_COOKIE_MAX_AGE,
            path="/",
            httponly=True,
            samesite="lax",
        )
    templates = _repo.demo_templates()
    # One repo-shared topology snapshot covers every template generation.  We
    # then project only deterministic sandboxes owned by this principal.
    metas = {row["id"]: row for row in _repo.list_project_metas()}
    enriched: list[dict[str, Any]] = []
    for template in templates:
        sandbox_id = _repo.demo_sandbox_id(owner_id, str(template["id"]))
        meta = metas.get(sandbox_id)
        generation = 0
        if meta is not None:
            if (
                (meta.get("config") or {}).get("project_kind") != "demo_session"
                or meta["acl"].get("owner") != owner_id
            ):
                raise ZeError(
                    "project_recovery_required",
                    "deterministic demo namespace belongs to another principal",
                    context={"project_id": sandbox_id},
                )
            generation = int(meta["generation"])
        enriched.append({
            **template,
            "sandbox_project_id": sandbox_id,
            "sandbox_generation": generation,
        })
    result["templates"] = enriched
    if not actor.user_id:
        result["guest_scope"] = _guest_scope(owner_id)
    return result


@app.get("/api/demo-templates/{template_id}/intents/{intent_id}")
def demo_intent_status(template_id: str, intent_id: str, request: Request,
                       response: Response,
                       actor: Actor = Depends(_get_actor)):
    """Reconcile a demo reset using the already-established actor/guest id."""
    if not any(item.get("id") == template_id for item in _repo.demo_templates()):
        raise ZeError("not_found", "demo template does not exist")
    response.headers["Cache-Control"] = "no-store"
    owner_id = actor.user_id or request.cookies.get(GUEST_COOKIE_NAME) or ""
    if not owner_id:
        # **必须在这里真的把 cookie 发出去。** 错误码 guest_session_required 被
        # error_projection 投影成「guest session established; retry the same intent」
        # (会话已建立,用同一操作重试)。原来这条分支只抛错、不下发 cookie,于是那句话
        # 是假的:前端照着「重试」再来一次,还是没 cookie,还是同一个 409 —— **死循环**。
        # 2026-07-27 用户在 System 控制台点示例卡踩到,服务日志里连着三条 409 就是它。
        # POST 那条同名分支(见下方 demo_intent_replace)本来就是这么做的,这里对齐它。
        resp = JSONResponse(
            status_code=409,
            content={"error": {
                "code": "guest_session_required",
                "message": "guest session established; retry the same intent",
            }},
        )
        resp.headers["Cache-Control"] = "no-store"
        resp.set_cookie(
            GUEST_COOKIE_NAME,
            f"g_{secrets.token_hex(12)}",
            max_age=GUEST_COOKIE_MAX_AGE,
            path="/",
            httponly=True,
            samesite="lax",
        )
        return resp
    project_id = _repo.demo_sandbox_id(owner_id, template_id)
    try:
        meta = _repo.project_meta(project_id)
    except ZeError as exc:
        if exc.code != "project_not_found":
            raise
    else:
        if meta["acl"].get("owner") != owner_id:
            raise ZeError("acl_denied", "无权查看该示例操作状态")
    status = _repo.project_intent_status(
        project_id, intent_id, kind="replace", principal_id=owner_id,
    )
    return status


@app.post("/api/demo-templates/{template_id}/open")
async def open_demo_template(template_id: str, request: Request,
                             actor: Actor = Depends(_get_actor)):
    """打开 / 重置一个示例沙盒(确定性隐藏项目,删旧建新 = 重置)。返回 project_id。

    免登录:登录用户落自己隔离沙盒(token 权威);匿名访客按 `ze_guest` cookie 落**每浏览器
    一个随机、不可枚举**的 guest 沙盒 —— 并发访客互不干扰、互不可重置/操作(沙盒 id 由 guest
    身份哈希而来,他人无从枚举)。template_id 不存在 → 404;数据化模板缺资产 → 404(不静默)。
    """
    if actor.user_id:
        owner_id = actor.user_id
        scope_actor = actor
    else:
        # 身份 cookie 必须在 mutation 之前已到达浏览器。若第一次 POST 的响应丢失时才
        # 一并发 cookie，重试会换 guest、换 sandbox，幂等边界随之漂移。
        owner_id = request.cookies.get(GUEST_COOKIE_NAME) or ""
        if not owner_id:
            response = JSONResponse(
                status_code=409,
                content={"error": {
                    "code": "guest_session_required",
                    "message": "guest session established; retry the same intent",
                }},
            )
            response.set_cookie(
                GUEST_COOKIE_NAME,
                f"g_{secrets.token_hex(12)}",
                max_age=GUEST_COOKIE_MAX_AGE,
                path="/",
                httponly=True,
                samesite="lax",
            )
            return response
        scope_actor = Actor(user_id=owner_id)              # 幂等 ledger 按 guest 区分,匿名互不撞

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    clean, body_key = _strip_body_idempotency_key(body)
    expected_generation = _request_expected_generation(
        request, clean, allow_zero=True,
    )
    clean = {
        **_without_expected_generation(clean),
        "expected_generation": expected_generation,
    }
    intent_id, _ = _require_topology_request(request, clean, body_key=body_key)

    sandbox_id = _repo.demo_sandbox_id(owner_id, template_id)

    def _do() -> dict[str, Any]:
        commit = _repo.open_demo_template(
            actor_user_id=owner_id,
            template_id=template_id,
            intent_id=intent_id,
            expected_generation=expected_generation,
            return_commit=True,
        )
        return {
            "project_id": commit.project_id,
            "intent_id": intent_id,
            "status": "committed",
            "mutation_outcome": "committed",
            "kind": "replace",
            "generation": commit.generation,
            "project_generation": commit.generation,
        }

    # 幂等 scope 折入模板内容版本:同一访客重复点同一示例仍幂等(返回同沙盒、不抖);但内容一变
    # (部署了新 scenario——增减实体 / 新增物体参考图),scope 变 → 缓存未命中 → _do 删旧建新 → 拿到
    # 新内容。否则访客第一次开后被幂等冻结在旧内容上,后续部署的新内容永不可见(刷新回登录页重进仍命中
    # 旧缓存)。content_version 计算失败不阻断开 demo(回退定值)。
    try:
        content_ver = _repo.demo_template_content_version(template_id)
    except Exception:
        content_ver = "v0"
    return _run_topology_idempotent(
        request, actor=scope_actor, scope=f"demo.open:cv={content_ver}",
        project_id=sandbox_id, body={"template_id": template_id, **clean},
        body_key=body_key, action=_do,
        intent_id=intent_id, kind="replace", target_id=sandbox_id,
    )


# ==============================================================
# 文档 / 命令 / 事件(均按 project 隔离 + 校权)
# ==============================================================

@app.get("/api/document")
def get_document(response: Response, ctx: Context = Depends(_require_member_leased)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    doc = ctx.doc
    with ctx.lock:
        # demo 会话:用当前代码声明的 schema(现取 memoize)覆盖文档固化的旧 schema,让部署的新字段/enum
        # (如模型下拉)立即生效,不必重开 demo 丢数据;普通项目仍用各自固化 schema(尊重 schema_version 迁移)。
        is_demo = (doc.config or {}).get("project_kind") == "demo_session"
        data_schema = _repo.current_data_schema() if is_demo else doc.schema
        types = sorted(set(data_schema.keys()) | set(doc.types()))
        return {
            "meta": {
                "id": doc.id,
                "name": doc.name,
                "schema_version": doc.schema_version,
                "collaboration_mode": doc.collaboration_mode,
                "save_policy": doc.save_policy,
                "effective_save_policy": doc.effective_save_policy,
                "auto_commit_enabled": doc.auto_commit_enabled,
                "generation": ctx.generation,
                "version": doc.version,
                "data_schema": data_schema,
                "acl": doc.acl,
                "config": doc.config,   # 含 project_kind:前端据此识别 demo 沙盒态(示例徽记/另存为)
                # R-C18:装载时若发现数据结构损坏, 给前端一个轻量摘要以决定是否显示"结构诊断"入口
                # (详情走 GET /api/diagnose)。无损坏 = None, 前端不显示入口。
                "schema_diagnosis": (
                    {"finding_count": ctx.schema_diagnosis.get("finding_count", 0)}
                    if getattr(ctx, "schema_diagnosis", None) else None
                ),
            },
            "entities": {et: [e.to_dict() for e in doc.list(et)] for et in types},
            # Broker publish happens inside the same ctx.lock as document save.
            # This cursor therefore closes GET(snapshot) -> SSE(subscribe): the
            # client subscribes with since_seq=event_cursor+1.
            "event_cursor": int(ctx.broker.stats()["last_seq"]),
            "event_epoch": str(ctx.broker.stats()["stream_id"]),
        }


# ==============================================================
# AI 最低接口(只读 introspection;reader 在 sdk/introspect.py,server+CLI 共用同形)
#   红线:只读,不产生 Operation、不写 OpLog。写仍走 POST /api/command/{id}。
# ==============================================================

@app.get("/api/context/describe")
def get_context_describe(ctx: Context = Depends(_require_member_leased)) -> dict:
    """Context.Describe — 对象图:Document/Entity/Editor/Command/Task/ai_flow。"""
    with ctx.lock:
        return describe_context(ctx, introspect=_repo.introspect, config=_repo.config)


@app.get("/api/diagnose")
def get_diagnose(response: Response, type: str = "", id: str = "",
                 ctx: Context = Depends(_require_member_leased)) -> dict:
    """Schema Doctor(R-C18)只读诊断:数据结构对不上时给逐处 path/expected/how_to_fix/ai_prompt。

    无参=全 doc 汇总;`?type=&id=`=单实体。reader 红线:只读、不产生 Operation/OpLog、
    不改 canonical;got 恒脱敏(redact=True)。
    """
    response.headers["Cache-Control"] = "no-store"
    with ctx.lock:
        if type and id:
            try:
                ent = ctx.doc.get(type, id)
            except ZeError:
                raise ZeError("entity_not_found", f"{type}/{id} 不存在",
                              context={"type": type, "id": id})
            schema = (ctx.doc.schema or {}).get(type) or {}
            return diagnose_build_report(
                getattr(ent, "data", None), schema, entity_type=type, entity_id=id,
                trigger="api", redact=True, path_prefix=f"{type}/{id}")
        # 开门时若已算过诊断(schema 损坏), 直接回那份, 免重算。
        if getattr(ctx, "schema_diagnosis", None):
            return ctx.schema_diagnosis
        return diagnose_document(ctx.doc, redact=True, trigger="api")


@app.post("/api/diagnose")
async def post_diagnose(request: Request, response: Response,
                        ctx: Context = Depends(_require_member_leased)) -> dict:
    """写前预检 / 写失败复诊(dry-run):body {type, value} 跑诊断,**不写任何东西**。"""
    response.headers["Cache-Control"] = "no-store"
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body 必须是 JSON 对象")
    etype = str(body.get("type") or "")
    schema = (ctx.doc.schema or {}).get(etype) or {}
    if not schema:
        raise ZeError("validation_failed", f"未知实体类型 {etype!r}",
                      context={"type": etype})
    return diagnose_build_report(body.get("value"), schema, entity_type=etype,
                                 trigger="write_dryrun", redact=True,
                                 path_prefix=f"{etype}/?")


@app.get("/api/context/tours")
def get_context_tours(ctx: Context = Depends(_require_member_leased)) -> dict:
    """Guided tours — 全量步(前端 GuideLayer 本地播『教我用』);summaries 已在 describe。
       只读投影 key_flows,不产生 Operation/OpLog。"""
    with ctx.lock:
        tours = compile_guided_tours(
            tool_understanding=_repo.introspect.tool_understanding,
            ai_flow=_repo.introspect.ai_flow,
            manual=_repo.introspect.manual,
            commands=list_commands(ctx.cmd_reg),
        )
    return {"tours": tours}


@app.get("/api/commands")
def get_commands(ctx: Context = Depends(_require_member_leased)) -> dict:
    """Command.List — 列出本 project 注册的命令(brief)与可选 pipeline 静态声明。"""
    with ctx.lock:
        return _with_project_generation({
            "commands": list_commands(ctx.cmd_reg),
            "pipelines": list_pipelines(_repo.introspect),
            "document_version": ctx.doc.version,
        }, ctx)


@app.get("/api/commands/{command_id}")
def get_command_describe(command_id: str, ctx: Context = Depends(_require_member_leased)) -> dict:
    """Command.Describe — 单命令完整声明(不存在 → unknown_command/404)。"""
    with ctx.lock:
        return _with_project_generation({
            **describe_command(ctx.cmd_reg, command_id),
            "document_version": ctx.doc.version,
        }, ctx)


@app.get("/api/editors")
def get_editors(ctx: Context = Depends(_require_member_leased)) -> dict:
    """Editor.List — mod 声明的编辑器映射 + core 算可运行命令 + renderer 注入需求。"""
    return {"editors": list_editors(_repo.introspect, ctx.cmd_reg)}


@app.get("/api/entities")
def get_entities(
    type: Optional[str] = Query(default=None),
    ctx: Context = Depends(_require_member_leased),
) -> dict:
    """Entity.List — 轻量列实体(id/name/version/deleted)。"""
    with ctx.lock:
        return {"entities": list_entities(ctx.doc, type)}


@app.get("/api/entity/select")
def get_entity_select(
    type: str = Query(...),
    id: str = Query(...),
    ctx: Context = Depends(_require_member_leased),
) -> dict:
    """Entity.Select — 只校验存在(view-state 概念,不改 canonical)。"""
    with ctx.lock:
        return entity_select(ctx.doc, type, id)


@app.get("/api/tasks")
def get_tasks(ctx: Context = Depends(_require_member_leased)) -> dict:
    """Task.List — 异步执行(管线 run)快照。"""
    return {"tasks": list_tasks(ctx.supervisor)}


@app.get("/api/tasks/{run_id}")
def get_task_status(run_id: str, ctx: Context = Depends(_require_member_leased)) -> dict:
    """Task.Status — 单 run 状态 + entity 级 item 状态。"""
    return task_status(ctx.supervisor, run_id)


@app.get("/api/oplog")
def get_oplog(
    type: Optional[str] = Query(default=None),
    id: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    ctx: Context = Depends(_require_member_leased),
) -> dict:
    """OperationLog.Query — 过滤审计轨迹 + 链完整性(/api/history 的具名过滤版)。"""
    with ctx.lock:
        return query_oplog(ctx.op_log, entity_type=type, entity_id=id,
                           source=source, limit=limit)


@app.get("/api/help/manual")
def get_help_manual(actor: Actor = Depends(_get_actor)) -> dict:
    """Help.ReadManual — mod 声明的说明书(skill 级,登录可读)。"""
    _require_tool_access(actor)
    return read_manual(_repo.introspect)


@app.post("/api/command/confirm")
async def post_command_confirm(
    request: Request,
    project_id: str = Query(...),
    actor: Actor = Depends(_get_actor),
):
    """确认一个待确认命令。body: {"token": "...", "approval_nonce": "..."}。
    注:本路由须声明在 /api/command/{command_id} 之前,否则会被后者以 command_id='confirm' 吃掉。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    clean, body_key = _strip_body_idempotency_key(body)
    expected_generation = _request_expected_generation(request, clean)
    clean = _without_expected_generation(clean)
    token = clean.get("token", "")
    approval_nonce = (
        clean.get("approval_nonce", "")
    )
    ledger_body = {**clean, "expected_generation": expected_generation}
    ctx, scope_actor = _idempotency_context_for(project_id, actor, request)

    def _do() -> dict[str, Any]:
        with ctx.lock:
            # Revalidate authorization only after joining the same critical
            # section as confirmation execution.  A collaborator revoked while
            # waiting for this lock must not consume a pending mutation.
            eff_actor = _require_edit_ctx(ctx, actor, request)
            with credential_context(
                eff_actor, _repo.credential_store, _repo.policy_store, _repo.tool_id,
            ):
                execution = execute_confirmation(
                    registry=ctx.cmd_reg,
                    coordinator=ctx.commit,
                    token=token,
                    actor=eff_actor,
                    document=ctx.doc,
                    approval_channel="human_http",
                    approval_nonce=str(approval_nonce or ""),
                )
            resp = _with_project_generation(
                commit_execution_payload(execution, document=ctx.doc), ctx,
            )
        if execution.is_committed:
            op = execution.operations[0]
            _usage.emit("feature_use", anon=anon_user(eff_actor.user_id),
                        command_id=op.command_id, feature_label=_command_label(ctx, op.command_id))
        return resp

    with ctx.lifecycle_guard():
        _require_context_generation(ctx, expected_generation)
        return _run_idempotent(
            request, actor=scope_actor, scope="command.confirm",
            project_id=project_id, body=ledger_body, body_key=body_key, action=_do,
            outcome_unknown_classifier=_commit_outcome_unknown,
        )


@app.get("/api/command/pending-approval")
def get_pending_approval(
    request: Request,
    project_id: str = Query(...),
    token: str = Query(...),
):
    """人工批准材料端点(浏览器 cookie 专属通道)。

    AI 发起的需确认命令经 SSE ``pending_confirmation`` 帧通知网页(帧不含 nonce);
    网页在此取批准材料(approval_nonce)后走既有 POST /api/command/confirm。
    鉴权**只认会话 cookie**——刻意忽略 Authorization 头与 query token:AI 子进程
    只持 Bearer session token、没有浏览器 cookie,结构上到不了这里、拿不到 nonce
    (COPILOT.md §6「MCP/AI 无批准能力」红线的 HTTP 面)。且只有 pending 的发起
    身份本人可取(与 PendingStore.reserve 的 actor 一致性校验同口径)。
    """
    cookie_actor = resolve_actor(
        authorization=None,
        cookie_token=request.cookies.get(SESSION_COOKIE_NAME),
        query_token=None,
        user_store=_repo.user_store,
        session_mgr=_repo.session_mgr,
    )
    ctx = _member_context_for(project_id, cookie_actor, request)
    with ctx.lock:
        _require_edit_ctx(ctx, cookie_actor, request)
        pending = ctx.cmd_reg.pending_store.peek(str(token or ""))
    if pending is None or str(pending.actor.get("user_id", "") or "") != cookie_actor.user_id:
        # 不区分「不存在/已过期/非本人」——统一 not found,不泄露他人 pending 的存在性。
        raise ZeError(
            "confirmation_not_found", "无此待确认命令(可能已过期或已处理)",
            context={"token": str(token or "")},
        )
    return {"pending": pending.to_public_dict(include_approval=True)}


@app.post("/api/command/cancel")
async def post_command_cancel(
    request: Request,
    project_id: str = Query(...),
    actor: Actor = Depends(_get_actor),
):
    """取消一个待确认命令(不执行)。body: {"token": "..."}。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    clean, body_key = _strip_body_idempotency_key(body)
    expected_generation = _request_expected_generation(request, clean)
    clean = _without_expected_generation(clean)
    token = clean.get("token", "")
    ledger_body = {**clean, "expected_generation": expected_generation}
    ctx, scope_actor = _idempotency_context_for(project_id, actor, request)

    def _do() -> dict[str, Any]:
        with ctx.lock:
            # Pending cancellation mutates shared command state, so its final
            # ACL decision belongs to the command lock, not the queueing phase.
            eff_actor = _require_edit_ctx(ctx, actor, request)
            return _with_project_generation({
                "cancelled": ctx.cmd_reg.pending_store.cancel(token, actor=eff_actor),
            }, ctx)

    with ctx.lifecycle_guard():
        _require_context_generation(ctx, expected_generation)
        return _run_idempotent(
            request, actor=scope_actor, scope="command.cancel",
            project_id=project_id, body=ledger_body, body_key=body_key, action=_do,
        )


@app.post("/api/command/{command_id}")
async def post_command(
    command_id: str,
    request: Request,
    project_id: str = Query(...),
    actor: Actor = Depends(_get_actor),
):
    try:
        args = await request.json()
    except Exception:
        args = {}
    if not isinstance(args, dict):
        raise ZeError("validation_failed", "request body must be a JSON object (command args)")
    args, body_key = _strip_body_idempotency_key(args)
    expected_generation = _request_expected_generation(request, args)
    args = _without_expected_generation(args)
    ledger_body = {"expected_generation": expected_generation, "args": args}
    ctx, scope_actor = _idempotency_context_for(project_id, actor, request)

    def _do() -> dict[str, Any]:
        with ctx.lock:
            # Authorization and mutation are one critical section.  Checking
            # before acquiring this lock creates a revoke TOCTOU: a collaborator
            # can pass the old ACL, wait behind document.set_acl, then write
            # after removal because entity CAS does not consume Document._v.
            eff_actor = _require_edit_ctx(ctx, actor, request)
            # 改协作者名单:demo 沙盒是一次性隐藏副本,禁改共享(server 专属:demo 仅 HTTP 可达)。
            # owner-only 校验已下沉命令层(builtin_commands._require_document_acl_owner),
            # HTTP/MCP/CLI 三通道共用同一门 —— 此处不再重复 owner 判定,避免双重报错语义。
            if command_id == "document.set_acl":
                if _is_demo_doc(ctx.doc):
                    raise ZeError("acl_denied", "示例沙盒不支持共享设置")

            def _produce():
                with credential_context(
                    eff_actor, _repo.credential_store, _repo.policy_store, _repo.tool_id,
                ):
                    return ctx.cmd_reg.dispatch(
                        command_id, args,
                        actor=eff_actor, source="user",
                        doc=ctx.doc, op_log=None,
                    )

            execution = ctx.commit.execute(_produce)
            resp = commit_execution_payload(
                execution, document=ctx.doc, include_approval=True,
            )
            resp = _with_project_generation(resp, ctx)
            # 需确认命令：producer 返回 PendingConfirmation，coordinator 保证零持久化/零事件。
            if execution.pending_confirmation is not None:
                return resp
        # 埋点:命令成功后记一次「功能使用」(锁外,非阻塞)。覆盖 user 源的全部动作。
        if execution.is_committed:
            _usage.emit("feature_use", anon=anon_user(eff_actor.user_id),
                        command_id=command_id, feature_label=_command_label(ctx, command_id))
        return resp

    with ctx.lifecycle_guard():
        _require_context_generation(ctx, expected_generation)
        return _run_idempotent(
            request, actor=scope_actor, scope=f"command.{command_id}",
            project_id=project_id, body=ledger_body, body_key=body_key, action=_do,
            outcome_unknown_classifier=_commit_outcome_unknown,
        )


def _history_commit(ctx: Context, *, actor: Actor, kind: str) -> dict:
    """把 undo/redo 的 apply、耐久、广播与本地历史游标收进同一提交边界。"""
    prepared: dict[str, Any] = {}

    def _produce():
        record = (
            ctx.undo.prepare_undo(actor=actor)
            if kind == "undo"
            else ctx.undo.prepare_redo(actor=actor)
        )
        prepared["record"] = record
        if record is None:
            return None
        if len(record.operations) == 1:
            return record.operations[0]
        return MutationBatch(record.operations)

    try:
        execution = ctx.commit.execute(_produce)
    except BaseException as original_error:
        record = prepared.get("record")
        if record is not None:
            # The mutation session restores Document state, but the history
            # controller owns a separate short-lived issuance/body ledger.
            # Release it before preserving the coordinator exception so a
            # rejected undo/redo cannot poison every later history request.
            try:
                ctx.undo.discard_deferred(record)
            except BaseException as cleanup_error:
                # FROZEN/unknown history is intentionally not convertible into
                # a clean discard.  Keep the controller ledger for repair and
                # preserve a typed authority error when it already states the
                # root cause; otherwise surface the failed cleanup.  Projection
                # belongs only to the HTTP exception handler so the idempotency
                # ledger can still inspect ``outcome_unknown`` here.
                if is_commit_authority_error(original_error):
                    raise original_error
                raise cleanup_error from original_error
        raise
    record = prepared.get("record")
    if record is not None:
        if execution.is_committed:
            ctx.undo.confirm_deferred(record)
        else:
            # 这里只释放历史凭据；Document 通用失败回滚由 W04 mutation session 接管。
            ctx.undo.discard_deferred(record)
    response = commit_execution_payload(execution, document=ctx.doc, many=True)
    response.update({"can_undo": ctx.undo.can_undo(), "can_redo": ctx.undo.can_redo()})
    return response


@app.post("/api/undo")
async def post_undo(request: Request, project_id: str = Query(...),
                    actor: Actor = Depends(_get_actor)):
    """撤销最末用户操作；undo 从落盘 OpLog 推导，redo 只保留当前进程会话。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    clean, body_key = _strip_body_idempotency_key(body)
    expected_generation = _request_expected_generation(request, clean)
    ledger_body = {
        **_without_expected_generation(clean),
        "expected_generation": expected_generation,
    }
    ctx, scope_actor = _idempotency_context_for(project_id, actor, request)

    def _do() -> dict[str, Any]:
        with ctx.lock:
            eff_actor = _require_edit_ctx(ctx, actor, request)
            return _with_project_generation(
                _history_commit(ctx, actor=eff_actor, kind="undo"), ctx,
            )
    with ctx.lifecycle_guard():
        _require_context_generation(ctx, expected_generation)
        return _run_idempotent(
            request, actor=scope_actor, scope="history.undo",
            project_id=project_id, body=ledger_body, body_key=body_key, action=_do,
            outcome_unknown_classifier=_commit_outcome_unknown,
        )


@app.post("/api/redo")
async def post_redo(request: Request, project_id: str = Query(...),
                    actor: Actor = Depends(_get_actor)):
    """重做最后一次撤销。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    clean, body_key = _strip_body_idempotency_key(body)
    expected_generation = _request_expected_generation(request, clean)
    ledger_body = {
        **_without_expected_generation(clean),
        "expected_generation": expected_generation,
    }
    ctx, scope_actor = _idempotency_context_for(project_id, actor, request)

    def _do() -> dict[str, Any]:
        with ctx.lock:
            eff_actor = _require_edit_ctx(ctx, actor, request)
            return _with_project_generation(
                _history_commit(ctx, actor=eff_actor, kind="redo"), ctx,
            )
    with ctx.lifecycle_guard():
        _require_context_generation(ctx, expected_generation)
        return _run_idempotent(
            request, actor=scope_actor, scope="history.redo",
            project_id=project_id, body=ledger_body, body_key=body_key, action=_do,
            outcome_unknown_classifier=_commit_outcome_unknown,
        )


@app.get("/api/history")
def get_history(ctx: Context = Depends(_require_member_leased)):
    """读 OpLog(操作日志)+ undo/redo 可用状态。前端 footer 刷新后可重建历史面板。"""
    with ctx.lock:
        return _with_project_generation({
            "entries": public_oplog_entries(ctx.op_log),
            "can_undo": ctx.undo.can_undo(),
            "can_redo": ctx.undo.can_redo(),
            "document_version": ctx.doc.version,
        }, ctx)


# 安全:sha256 只能是 64 hex;ext 只能是 .字母数字(≤16);mime 去控制字符 + 限长。
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,16}\Z")
_DEFAULT_HTTP_ASSET_UPLOAD_MAX_BYTES = 32 * 1024 * 1024


def _safe_mime(raw: Optional[str]) -> str:
    mime = (raw or "application/octet-stream").split("\r")[0].split("\n")[0].strip()
    return mime[:127] if mime else "application/octet-stream"


def _http_asset_upload_max_bytes() -> int:
    value = _repo.config.get(
        "ze.http.asset_upload_max_bytes",
        _DEFAULT_HTTP_ASSET_UPLOAD_MAX_BYTES,
    )
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ZeError(
            "capacity_config_invalid",
            "asset upload byte limit must be a positive integer",
            context={"field": "ze.http.asset_upload_max_bytes"},
        )
    return value


def _asset_upload_too_large(limit: int, actual: int) -> ZeError:
    return ZeError(
        "asset_upload_too_large",
        f"asset upload exceeds HTTP limit ({limit} bytes)",
        context={"limit": limit, "actual": actual},
    )


async def _read_limited_asset_body(request: Request) -> bytes:
    """Read an upload once while enforcing both declared and streamed size."""
    limit = _http_asset_upload_max_bytes()
    declared = request.headers.get("content-length")
    if declared not in (None, ""):
        try:
            declared_size = int(declared)
        except (TypeError, ValueError) as exc:
            raise ZeError("validation_failed", "invalid Content-Length") from exc
        if declared_size < 0:
            raise ZeError("validation_failed", "invalid Content-Length")
        if declared_size > limit:
            raise _asset_upload_too_large(limit, declared_size)

    # One contiguous buffer keeps memory O(limit).  A list of attacker-chosen
    # one-byte chunks would otherwise spend far more memory on Python object
    # overhead than the configured byte limit accounts for.
    body = bytearray()
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise _asset_upload_too_large(limit, total)
        body.extend(chunk)
    return bytes(body)


# asset 内联安全白名单:仅这些类型允许浏览器内联渲染(图片/视频/音频要能直接 <img>/<video> 显示)。
# 其余(text/html、application/xhtml+xml、image/svg+xml 等可内嵌脚本的)一律强制附件下载 —— 否则
# 协作者上传 text/html blob,受害者打开 /api/asset/<sha> 会在工具同源执行脚本(存储型 XSS)、驱动
# /api/command 带受害者 cookie 越权。svg 虽属 image/* 但能内嵌 <script>,单独排除。
_INLINE_SAFE_MIME_PREFIXES = ("image/", "video/", "audio/")
_INLINE_SAFE_MIME_EXACT = frozenset({"application/pdf", "text/plain"})


def _asset_disposition(mime: str) -> str:
    """决定 asset 响应用 inline 还是 attachment(配合 nosniff 防存储型 XSS)。"""
    m = mime.split(";")[0].strip().lower()
    if m.startswith("image/svg"):   # image/svg+xml 及裸 image/svg 都能内嵌 <script>,强制下载
        return "attachment"
    if m in _INLINE_SAFE_MIME_EXACT or any(m.startswith(p) for p in _INLINE_SAFE_MIME_PREFIXES):
        return "inline"
    return "attachment"


class _RangeNotSatisfiable(Exception):
    """单段 bytes Range 语法合法但完全落在 blob 之外(RFC 7233 → 416)。"""


def _parse_byte_range(range_header: str, size: int) -> "tuple[int, int] | None":
    """解析单段 bytes Range,可满足时返回闭区间 (start, end)。

    <video>/<audio> seek 的前提:浏览器先发 bytes=0- 探测,拖动进度条后按目标
    位置发子段;服务端不答 206 时 Chromium 判定流不可 seek(seekable=[[0,0]])。
    不可解析/倒序/非 bytes 单位/多段 → 返回 None(RFC 7233 允许忽略,整体 200);
    语法合法但不可满足(起点越界、空 suffix)→ 抛 _RangeNotSatisfiable(416)。
    """
    unit, _, spec = range_header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    start_s, sep, end_s = spec.strip().partition("-")
    if not sep:
        return None
    start_s, end_s = start_s.strip(), end_s.strip()
    try:
        if not start_s:                        # 后缀式 bytes=-N:最后 N 字节
            suffix = int(end_s)
            if suffix <= 0:
                raise _RangeNotSatisfiable()
            start, end = max(0, size - suffix), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
            if start < 0 or (end_s and end < start):
                return None
    except ValueError:
        return None
    if start >= size:
        raise _RangeNotSatisfiable()
    return start, min(end, size - 1)


# _scan_durable_asset_refs / _durable_asset_access / _asset_ref_for_read_locked 已迁 sdk.asset_access
# (HTTP 与 MCP 共用同一道 durable ACL 闸门),此处以别名 import 复用,见文件顶部。


def _durable_media_principal(
    ctx: Context,
    request: Request,
    actor: Actor,
    acl: dict[str, Any],
    config: dict[str, Any],
) -> str:
    """Re-authorize a ticket issuer/direct reader against durable metadata."""
    if config.get("project_kind") == "demo_session":
        principal = actor.user_id or request.cookies.get(GUEST_COOKIE_NAME) or ""
        owner = str(acl.get("owner") or "")
        if not principal or not owner or principal != owner:
            raise ZeError("acl_denied", "no access to this demo session")
        return principal
    _require_tool_access(actor)
    if not _can_access(acl, actor.user_id):
        raise ZeError("acl_denied", "no access to this project")
    return actor.user_id


def _media_ticket_subject_allowed(
    subject: str,
    acl: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    """Apply the current durable ACL without exposing the principal in a URL."""
    owner = str(acl.get("owner") or "")
    collaborators = acl.get("collaborators") or []
    if config.get("project_kind") == "demo_session":
        candidates = (owner,) if owner else ()
    elif not owner:
        # The ticket could only be minted by an authenticated tool user.  An
        # ownerless document retains its existing logged-in-public semantics.
        return bool(subject)
    else:
        candidates = (owner, *collaborators)
    for principal in candidates:
        if not isinstance(principal, str) or not principal:
            continue
        expected = _repo.media_tickets.subject_for(principal)
        if secrets.compare_digest(subject, expected):
            return True
    return False


@app.post("/api/asset-tickets")
async def issue_asset_tickets(
    request: Request,
    response: Response,
    ctx: Context = Depends(_require_member_leased),
    actor: Actor = Depends(_get_actor),
):
    """Mint short asset-scoped capabilities using normal authenticated fetch."""
    response.headers["Cache-Control"] = "private, no-store"
    body = await request.json()
    if not isinstance(body, dict):
        raise ZeError("validation_failed", "body must be a JSON object")
    generation = _request_expected_generation(request, body)
    _require_context_generation(ctx, generation)
    raw_shas = body.get("sha256s")
    if (
        not isinstance(raw_shas, list) or not raw_shas or len(raw_shas) > 512
        or any(not isinstance(item, str) or not _SHA256_RE.fullmatch(item) for item in raw_shas)
        or len(set(raw_shas)) != len(raw_shas)
    ):
        raise ZeError(
            "validation_failed",
            "sha256s must be a unique non-empty list of at most 512 checksums",
        )

    with ctx.lock:
        acl, config, durable_refs = _durable_asset_access(ctx)
        principal = _durable_media_principal(ctx, request, actor, acl, config)
        available = [
            sha for sha in raw_shas
            if _asset_ref_for_read_locked(ctx, sha, durable_refs) is not None
        ]
        tickets: dict[str, str] = {}
        # Missing refs are an authoritative negative result for the same short
        # window.  Give all results one batch clock so an all-missing response
        # cannot degrade into a client-side one/five-second retry loop.
        batch_now = time.time()
        expires_at = _repo.media_tickets.expires_at(now=batch_now)
        for sha in available:
            ticket, expiry = _repo.media_tickets.issue(
                principal_id=principal,
                project_id=ctx.doc.id,
                generation=ctx.generation,
                sha256=sha,
                now=batch_now,
            )
            tickets[sha] = ticket
            expires_at = min(expires_at, expiry)
    return _with_project_generation({
        "tickets": tickets,
        "expires_at": expires_at,
        "missing": [sha for sha in raw_shas if sha not in tickets],
    }, ctx)


@app.get("/api/asset/{sha256}")
def get_asset(
    sha256: str,
    request: Request,
    project_id: str = Query(...),
    generation: Optional[str] = Query(default=None),
    ticket: Optional[str] = Query(default=None),
    actor: Actor = Depends(_get_header_cookie_actor),
):
    """按 sha256 返回 blob(内容寻址;per-project 隔离 + 校权,防跨项目越权读图)。

    共享池(P3):本地无、共享池有的 blob,仅当**本 project 实际引用了它**才放行 ——
    否则 404。**本地优先且不引用就不查共享池** → 未引用的共享 sha 连共享 meta stat 都不触发,
    杜绝「都是 404 但响应时间侧信道泄露存在性」。doc 扫描持 ctx.lock 取一致快照(防 doc 并发改时
    撕裂读),blob 内容寻址不可变 → 锁外发送。
    """
    _404 = JSONResponse(status_code=404,
                        content={"error": {"code": "asset_not_found", "message": sha256}})
    if not _SHA256_RE.match(sha256):
        return JSONResponse(status_code=404,
                            content={"error": {"code": "asset_not_found", "message": "bad sha"}})
    claims = None
    if ticket:
        if generation is None:
            raise ZeError("media_ticket_invalid", "media ticket is invalid or expired")
        try:
            ticket_generation = _parse_project_generation(generation)
        except ZeError as exc:
            raise ZeError("media_ticket_invalid", "media ticket is invalid or expired") from exc
        # Authenticate the capability before an anonymous request can load or
        # probe an arbitrary project namespace.
        claims = _repo.media_tickets.verify(
            ticket,
            project_id=project_id,
            generation=ticket_generation,
            sha256=sha256,
        )
        ctx = _repo.get_or_load(project_id)
    else:
        # Backward-compatible direct reads may use Authorization/cookie, but URL
        # query session tokens are intentionally ignored by this dependency.
        ctx = _member_context_for(project_id, actor, request)

    with ctx.lifecycle_guard():
        if claims is not None:
            _require_context_generation(ctx, claims.generation)
            with ctx.lock:
                acl, config, durable_refs = _durable_asset_access(ctx)
                if not _media_ticket_subject_allowed(claims.subject, acl, config):
                    raise ZeError("acl_denied", "media ticket principal no longer has access")
                ref = _asset_ref_for_read_locked(ctx, sha256, durable_refs)
        else:
            if generation is not None:
                _require_context_generation(ctx, _parse_project_generation(generation))
            with ctx.lock:
                acl, config, durable_refs = _durable_asset_access(ctx)
                _durable_media_principal(ctx, request, actor, acl, config)
                ref = _asset_ref_for_read_locked(ctx, sha256, durable_refs)
        if ref is None:
            return _404
        try:
            data = ctx.asset_store.get(ref)
        except ZeError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise ZeError(
                "asset_corrupted", "asset metadata/blob cannot be read",
                context={"sha256": sha256},
            ) from exc
    mime = _safe_mime(ref.mime)
    headers = {
        "Cache-Control": "private, no-store, max-age=0",
        "Pragma": "no-cache",
        "Vary": "Authorization, Cookie",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": _asset_disposition(mime),
        "Accept-Ranges": "bytes",
    }
    # Range 处理在两条鉴权路径(media_ticket / 直连)汇合之后 → ticket 播放器
    # 发的 Range 同样生效。blob 已整读并过 checksum,切片只省网络不降完整性。
    range_header = request.headers.get("range")
    if range_header:
        try:
            span = _parse_byte_range(range_header, len(data))
        except _RangeNotSatisfiable:
            headers["Content-Range"] = f"bytes */{len(data)}"
            return Response(status_code=416, headers=headers)
        if span is not None:
            start, end = span
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            return Response(
                content=data[start:end + 1], status_code=206,
                media_type=mime, headers=headers,
            )
    return Response(content=data, media_type=mime, headers=headers)


@app.post("/api/asset")
async def put_asset(
    request: Request,
    ctx: Context = Depends(_require_member),
    actor: Actor = Depends(_get_actor),
):
    """上传 blob 到当前 project 的 asset store(raw body;ext/mime 严校)。"""
    # Cheap advisory preflight before accepting a potentially large/chunked
    # body.  This is not the authority decision: ACL can change while the body
    # is in flight, so the exact same check is repeated under ``ctx.lock`` just
    # before touching the generation-bound AssetStore.
    _require_edit_ctx(ctx, actor, request)
    # Do not hold a topology lease while a slow client is still uploading.
    # The short lease below revalidates both generation and edit authority
    # immediately before touching the generation-bound AssetStore.
    data = await _read_limited_asset_body(request)
    if not data:
        raise ZeError("validation_failed", "empty body")
    ext = request.query_params.get("ext", "")
    if ext and not _EXT_RE.match(ext):
        raise ZeError("validation_failed", "ext 必须是 .字母数字(≤16)")
    mime = _safe_mime(request.headers.get("content-type"))
    capacity = CapacityChecker(CapacityLimits.from_env())
    capacity.check_asset_blob(len(data))
    sha256 = hashlib.sha256(data).hexdigest()
    expected_generation = _request_expected_generation(request)
    with ctx.lifecycle_guard():
        _require_context_generation(ctx, expected_generation)
        with ctx.lock:
            # Authoritative, lock-coupled edit check.  Passing the preflight is
            # never permission to finish after an owner revokes this actor.
            # demo 沙盒按「可自由编辑、重开即重置」处理,允许上传参考图/替换图;普通项目仍要写权。
            _require_edit_ctx(ctx, actor, request)
            try:
                existing = ctx.asset_store.local_ref_for(sha256)
            except (TypeError, ValueError, OSError) as exc:
                raise ZeError(
                    "asset_corrupted", "existing asset metadata is invalid",
                    context={"sha256": sha256},
                ) from exc
            if existing is not None:
                if existing.size != len(data) or not ctx.asset_store.is_local(existing):
                    raise ZeError(
                        "asset_corrupted", "existing asset metadata/blob disagree",
                        context={"sha256": sha256},
                    )
                ref = existing
            else:
                capacity.check_doc_total_assets(
                    ctx.asset_store, additional_size=len(data),
                )
                ref = ctx.asset_store.put(data, mime=mime, ext=ext)
            return _with_project_generation(ref.to_dict(), ctx)


# .ze-pack 文件名白名单(manifest.export 产名:<YYYYMMDD-HHMMSS>-<域>-<uuid6>.ze-pack);无路径分隔符
_PACK_NAME_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z]+-[0-9a-f]{6}\.ze-pack$")


@app.get("/api/manifest/file/{name}")
def get_manifest_file(
    name: str,
    request: Request,
    ctx: Context = Depends(_require_member_leased),
):
    """下载本 project 的 .ze-pack 导出件(member-gated;名字严格白名单防穿越;404 不回显名字)。"""
    expected_generation = _request_expected_generation(request)
    _require_context_generation(ctx, expected_generation)
    if not _PACK_NAME_RE.match(name):
        return JSONResponse(status_code=404,
                            content={"error": {"code": "manifest_not_found", "message": "not found"}})
    path = ctx.data_dir / "exports" / name
    if not path.is_file():
        return JSONResponse(status_code=404,
                            content={"error": {"code": "manifest_not_found", "message": "not found"}})
    return Response(content=path.read_bytes(), media_type="application/zip",
                    headers={
                        "Content-Disposition": f'attachment; filename="{name}"',
                        "X-Project-Generation": str(ctx.generation),
                        "Cache-Control": "no-store",
                    })


# 声明式 FormAction project export：首个真实消费者只开放 JSON。物理目录、路由、MIME
# 均由框架固定；mod 只能在 Operation intent 里返回一个符合本闭集的文件名。
_PROJECT_EXPORT_NAME_RE = re.compile(
    r"^[0-9]{8}-[0-9]{6}-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{12}\.json$"
)
_MAX_PROJECT_EXPORT_BYTES = 64 * 1024 * 1024


def _project_export_not_found() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": {"code": "export_not_found", "message": "not found"}},
    )


def _read_project_export(ctx: Context, name: str) -> bytes | None:
    """Open one regular export beneath the fixed directory without following links.

    ``dir_fd + O_NOFOLLOW + fstat`` keeps validation and opening on the same inode. O_NONBLOCK
    prevents a malicious FIFO from hanging a request before the regular-file check.
    """
    if _PROJECT_EXPORT_NAME_RE.fullmatch(name) is None or not hasattr(os, "O_NOFOLLOW"):
        return None
    directory_fd = -1
    file_fd = -1
    try:
        directory_flags = (
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        directory_fd = os.open(ctx.data_dir / "exports", directory_flags)
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            return None
        file_flags = (
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        file_fd = os.open(name, file_flags, dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 \
                or info.st_size > _MAX_PROJECT_EXPORT_BYTES \
                or info.st_nlink != 1 or info.st_uid != os.geteuid():
            return None
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(file_fd, min(1024 * 1024, remaining))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        # A writer must publish by atomic no-overwrite promotion; reject any file whose size
        # changed around this read instead of serving a mixed/partial result.
        after = os.fstat(file_fd)
        if after.st_dev != info.st_dev or after.st_ino != info.st_ino \
                or after.st_size != info.st_size or after.st_mtime_ns != info.st_mtime_ns \
                or after.st_nlink != 1 or after.st_uid != os.geteuid():
            return None
        return b"".join(chunks)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError, TypeError, ValueError):
        return None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


@app.get("/api/export/file/{name}")
def get_project_export_file(
    name: str,
    request: Request,
    ctx: Context = Depends(_require_member_leased),
):
    """Download one framework-managed JSON export from the current project generation."""
    expected_generation = _request_expected_generation(request)
    _require_context_generation(ctx, expected_generation)
    content = _read_project_export(ctx, name)
    if content is None:
        return _project_export_not_found()
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Project-Generation": str(ctx.generation),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _stream_actor(request: Request) -> Actor:
    """Resolve session state afresh; an SSE handshake Actor may expire later."""
    return _get_transport_actor(request, request.query_params.get("token"))


def _assert_stream_access_locked(ctx: Context, request: Request) -> None:
    """Revalidate current policy and the durable ACL while ctx.lock is held."""
    actor = _stream_actor(request)
    metadata = read_document_metadata(ctx.doc_path)
    if metadata.id != ctx.doc.id:
        raise ZeError(
            "project_recovery_required", "project identity metadata is invalid",
            context={"project_id": ctx.doc.id, "reason": "document_id_mismatch"},
        )
    acl = validate_document_acl(
        metadata.acl,
        error_code="project_recovery_required",
        context={"project_id": ctx.doc.id},
    )
    config = metadata.config
    if config.get("project_kind") == "demo_session":
        principal = actor.user_id or request.cookies.get(GUEST_COOKIE_NAME) or ""
        if not principal or principal != acl.get("owner"):
            raise ZeError("acl_denied", "demo session access was revoked")
    else:
        _require_tool_access(actor)
        if not _can_access(acl, actor.user_id):
            raise ZeError("acl_denied", "project access was revoked")
    # A different process may atomically save the same topology generation but
    # cannot publish into this process-local broker.  Never pretend that stream
    # is complete: force a canonical refetch/reconnect.
    disk_version = metadata.version
    if disk_version != int(ctx.doc.version):
        raise ZeError(
            "project_generation_stale",
            "project context changed in another process",
            context={"project_id": ctx.doc.id},
        )


def _sse_initial_snapshot(
    ctx: Context, request: Request,
) -> tuple[str, dict[str, Any], dict[str, Any], int]:
    """Read one generation/access-consistent SSE snapshot in a worker thread."""
    with ctx.lifecycle_guard():
        with ctx.lock:
            _assert_stream_access_locked(ctx, request)
            doc_id = ctx.doc.id
            # Pipeline broadcasts can occur just after releasing their state
            # lock, not necessarily while ctx.lock is held.  Accept a full
            # snapshot only inside a stable broker-cursor window; otherwise an
            # event could publish between snapshot and cursor sampling and an
            # older full snapshot would be emitted after its newer replay.
            for _attempt in range(8):
                cursor_before = int(ctx.broker.stats()["last_seq"])
                pipe_snap = to_pipeline_status_payload(
                    ctx.supervisor.snapshot(), doc_id=doc_id,
                )
                stat_snap = to_status_payload(
                    ctx.supervisor.entity_status_snapshot(), doc_id=doc_id,
                )
                # This frame is an authoritative replacement snapshot, not an
                # incremental runtime delta.  In particular, completed items
                # are intentionally absent from entity_status_snapshot(); the
                # client must therefore remove keys missing from this frame.
                stat_snap["replace"] = True
                cursor_after = int(ctx.broker.stats()["last_seq"])
                if cursor_before == cursor_after:
                    return doc_id, pipe_snap, stat_snap, cursor_after
    raise ZeError(
        "project_busy", "realtime status changed too quickly to snapshot safely",
        context={"project_id": ctx.doc.id},
    )


def _probe_project_generation(ctx: Context, request: Request | None = None) -> int:
    """Short blocking generation+authorization fence for a worker thread."""
    with ctx.lifecycle_guard():
        if request is not None:
            with ctx.lock:
                _assert_stream_access_locked(ctx, request)
        return int(ctx.generation)


async def _probe_project_generation_async(
    ctx: Context, request: Request | None = None,
) -> int:
    """Async-loop-safe wrapper for the blocking filesystem generation fence."""
    return await asyncio.to_thread(_probe_project_generation, ctx, request)


def _stream_control_payload(ctx: Context, kind: str, reason: str) -> dict[str, Any]:
    return _with_event_epoch({
        "type": kind,
        "reason": reason,
        "event_cursor": int(ctx.broker.stats()["last_seq"]),
    }, ctx)


@app.get("/api/events")
async def events(
    request: Request,
    since_seq: int = Query(default=0, ge=0),
    event_epoch: Optional[str] = Query(default=None),
    ctx: Context = Depends(_require_transport_member),
    actor: Actor = Depends(_get_transport_actor),
):
    """SSE: 把该 project 的 Broker entity_change 实时推给前端(连接即推快照)。"""
    loop = asyncio.get_running_loop()
    incoming: deque[dict[str, Any]] = deque()
    incoming_lock = threading.Lock()
    wake = asyncio.Event()
    overflowed = threading.Event()

    def _enqueue(payload: dict) -> None:
        with incoming_lock:
            if len(incoming) >= 256:
                # Dropping one frame makes every later frame ambiguous. Mark
                # the stream terminal; generator emits resync_required.
                overflowed.set()
            else:
                incoming.append(payload)
        loop.call_soon_threadsafe(wake.set)

    def _cb(payload: dict) -> None:
        # Synchronous append is the snapshot barrier: once a publisher releases
        # ctx.lock, every seq it published is already visible in ``incoming``.
        _enqueue(payload)

    def _pop_nowait(*, through_seq: int | None = None) -> dict | None:
        with incoming_lock:
            if not incoming:
                return None
            if through_seq is not None and int(incoming[0].get("seq") or 0) > through_seq:
                return None
            return incoming.popleft()

    async def _next_payload(timeout: float) -> dict | None:
        deadline = loop.time() + timeout
        while True:
            payload = _pop_nowait()
            if payload is not None:
                return payload
            with incoming_lock:
                if incoming:
                    continue
                wake.clear()
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(wake.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    sub = ctx.broker.subscribe(callback=_cb, doc_id=ctx.doc.id, since_seq=since_seq)

    async def _terminal_control_after_probe() -> dict[str, Any] | None:
        try:
            await _probe_project_generation_async(ctx, request)
            return None
        except ZeError as exc:
            if exc.code in ("auth_required", "auth_failed", "acl_denied"):
                return _stream_control_payload(
                    ctx, "access_revoked", "authorization_changed",
                )
            if exc.code in (
                "project_busy", "project_not_found",
                "project_generation_stale", "project_recovery_required",
            ):
                return _stream_control_payload(
                    ctx, "resync_required", "generation_changed",
                )
            raise

    async def gen():
        try:
            yield ": connected\n\n"
            current_epoch = str(ctx.broker.stats()["stream_id"])
            if event_epoch and event_epoch != current_epoch:
                payload = _stream_control_payload(
                    ctx, "resync_required", "event_epoch_changed",
                )
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
            if getattr(sub, "replay_gap", False):
                payload = _stream_control_payload(
                    ctx, "resync_required",
                    getattr(sub, "gap_reason", "") or "replay_buffer_gap",
                )
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
            if overflowed.is_set():
                payload = _stream_control_payload(ctx, "resync_required", "queue_overflow")
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
            # 每次连接先声明 project generation。相同 project_id 的 demo reset 会换代；
            # 客户端据此 refetch canonical document，而不是继续显示旧 Context 快照。
            try:
                doc_id, pipe_snap, stat_snap, snapshot_cursor = await asyncio.to_thread(
                    _sse_initial_snapshot, ctx, request,
                )
            except ZeError as exc:
                kind = (
                    "access_revoked"
                    if exc.code in ("auth_required", "auth_failed", "acl_denied")
                    else "resync_required"
                )
                reason = "authorization_changed" if kind == "access_revoked" else "generation_changed"
                payload = _stream_control_payload(ctx, kind, reason)
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
            wait_until_delivered = getattr(sub, "wait_until_delivered", None)
            if callable(wait_until_delivered):
                settled = await asyncio.to_thread(
                    wait_until_delivered, snapshot_cursor, 1.0,
                )
                if not settled:
                    payload = _stream_control_payload(
                        ctx, "resync_required", "delivery_barrier_timeout",
                    )
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    return
            if overflowed.is_set():
                payload = _stream_control_payload(ctx, "resync_required", "queue_overflow")
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
            control = await _terminal_control_after_probe()
            if control is not None:
                yield f"data: {json.dumps(control, ensure_ascii=False)}\n\n"
                return
            generation_snap = _with_event_epoch({
                "type": "project_generation",
                "doc_id": doc_id,
                "generation": ctx.generation,
            }, ctx)
            yield f"data: {json.dumps(generation_snap, ensure_ascii=False)}\n\n"
            # Snapshot was captured at snapshot_cursor.  First deliver every
            # replay/live frame through that cursor, then place the full runtime
            # snapshots after them.  Events published later remain queued and
            # can no longer roll the snapshots backward.
            while True:
                payload = _pop_nowait(through_seq=snapshot_cursor)
                if payload is None:
                    break
                control = await _terminal_control_after_probe()
                if control is not None:
                    yield f"data: {json.dumps(control, ensure_ascii=False)}\n\n"
                    return
                fenced = _with_event_epoch(payload, ctx)
                yield f"data: {json.dumps(fenced, ensure_ascii=False)}\n\n"
            control = await _terminal_control_after_probe()
            if control is not None:
                yield f"data: {json.dumps(control, ensure_ascii=False)}\n\n"
                return
            yield f"data: {json.dumps(_with_event_epoch(pipe_snap, ctx), ensure_ascii=False)}\n\n"
            control = await _terminal_control_after_probe()
            if control is not None:
                yield f"data: {json.dumps(control, ensure_ascii=False)}\n\n"
                return
            yield f"data: {json.dumps(_with_event_epoch(stat_snap, ctx), ensure_ascii=False)}\n\n"
            last_ping = loop.time()
            while True:
                if await request.is_disconnected():
                    break
                if overflowed.is_set():
                    payload = _stream_control_payload(ctx, "resync_required", "queue_overflow")
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    return
                try:
                    payload = await _next_payload(1.0)
                except asyncio.CancelledError:
                    raise
                # Probe after the wait so an event queued during reset/revoke
                # cannot be emitted without one final current-policy fence.
                control = await _terminal_control_after_probe()
                if control is not None:
                    yield f"data: {json.dumps(control, ensure_ascii=False)}\n\n"
                    return
                if overflowed.is_set():
                    payload = _stream_control_payload(ctx, "resync_required", "queue_overflow")
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    return
                if payload is not None:
                    fenced = _with_event_epoch(payload, ctx)
                    yield f"data: {json.dumps(fenced, ensure_ascii=False)}\n\n"
                else:
                    if loop.time() - last_ping >= 15.0:
                        yield ": ping\n\n"        # keepalive
                        last_ping = loop.time()
        finally:
            sub.unsubscribe()

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ==============================================================
# Mod-owned service routes(optional;registered before catch-all static mount)
# ==============================================================
try:
    _mount_mod_server_routes()
except Exception:        # noqa: BLE001  —— mod service routes are optional; keep core web/API alive
    import traceback
    traceback.print_exc()


# ==============================================================
# MCP surface(第四 transport;ZE_SURFACE.json 声明了 mcp 才挂)
# ==============================================================
# 把同一套命令/introspection 经 MCP-over-HTTP 暴露给外部 agent(Claude Code/Codex/Cursor)。
# 必须在下方 catch-all mount 之前注册(显式路由优先);挂载失败不拖垮主服务。
try:
    from sdk.mcp_bridge import register_mcp_routes, mcp_surface_enabled
    if mcp_surface_enabled():
        register_mcp_routes(app, _repo)
except Exception:        # noqa: BLE001  —— MCP 是附属 transport,出错只记录、不影响 api/web
    import traceback
    traceback.print_exc()


# ==============================================================
# 静态前端托管(生产态:单端口同源)
# ==============================================================
# webapp/dist 存在时(start.sh 非 ZE_DEV 路径会 npm run build 产出),把构建后的 SPA 挂在 "/"。
# **必须在所有 /api、/health 路由之后 mount** —— app.mount("/") 是 catch-all,显式路由优先匹配。
# dev 态(ZE_DEV=1,vite 在 5173 单独跑)dist 不存在 → 不挂,后端纯 API。
_DIST = Path(__file__).resolve().parent / "webapp" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="webapp")
