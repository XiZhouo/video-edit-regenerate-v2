"""sdk/model.py — 模型调用薄驱动(深化点 #1:补全 config 已声明路线的执行层).

`04-ops` 的 `seed_framework_config` 已声明 LLM 路线(`ze.llm.route` = claude-cli /
codex-cli / gateway)与图片服务(`ze.image.*`),但只有声明、没有驱动——每个生成型
work skill 都得自己重写网关/子进程调用。本模块把「按 config 路线分发」收成一组薄函数,
server 路由、CLI、mod 的 stage 共用同一调用层。

红线(防漂移护栏,见 FUTURE_STATE §6.1):
  - 本模块只做**机制**:按路线分发 + 基础 retry + I/O。模型适配 / prompt-pack /
    双语策略 / 结构化 schema 全是 **mod 策略**,不进这里。
  - 不 import 任何模型 SDK、不引入第三方 HTTP 库(只用 stdlib `urllib` / `subprocess`)。
  - 自适应 429 退避 / key-pool 属深化点 #2(管线),本模块保持薄,只做固定次数 retry。

约定(实测自既有工具):
  - gateway:OpenAI 兼容 `POST {base}/chat/completions`,Bearer 鉴权,取
    `choices[0].message.content`(同 ZE-image-prompt-studio/llm_client)。
  - 图片服务:keyless `POST {url}/v1/images/generations`(改图走 `/v1/images/edits`
    带 `images:[dataURI]`),返回 `data:[{url}|{b64_json}]`(同 image-gen/call_image_service)。
"""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_ACCOUNT_RE = re.compile(r"[A-Za-z0-9_@.\-]+")

from entity import ZeError  # 叶子直接 import(已由 sdk/_paths 注入路径;避免 barrel 循环)
from auth import MODEL_SCOPE_LLM, MODEL_SCOPE_IMAGE  # noqa: E402
from credentials import (  # noqa: E402
    CredentialStore, current_credential_context,
    PROVIDER_HUPO_GATEWAY, PROVIDER_OPENAI_API, PROVIDER_ANTHROPIC_API,
    PROVIDER_CODEX_CLI, PROVIDER_CLAUDE_CLI, PROVIDER_CURSOR_API, PROVIDER_GEMINI_API,
    META_HOME_DIR, cli_home_env,
)

# 路线缺省模型(config `ze.llm.model` 留空时):claude→sonnet / codex→gpt / 网关→opus。
_ROUTE_DEFAULT_MODEL = {
    "claude-cli": "sonnet",
    "codex-cli": "gpt-5-codex",
    "gateway": "claude-opus-4-8",
}


# ==============================================================
# retry(薄:固定次数 + 线性退避;非自适应 429,那属深化点 #2)
# ==============================================================

def _with_retry(fn, *, retries: int, base_delay: float = 1.5):
    last: Exception | None = None
    for attempt in range(max(1, retries + 1)):
        try:
            return fn()
        except ZeError:
            raise                                  # 配置/校验类错误不重试
        except Exception as e:                     # noqa: BLE001 — 瞬时 I/O 失败才重试
            last = e
            if attempt < retries:
                time.sleep(base_delay * (attempt + 1))
    raise ZeError("model_call_failed", f"调用失败(重试 {retries} 次后): {last}",
                  context={"error": str(last)})


# ==============================================================
# LLM(llm_call — 按 ze.llm.route 分发)
# ==============================================================

def llm_call(config: Any, prompt: str, *, system: str | None = None,
             model: str | None = None, temperature: float = 0.7,
             max_tokens: int = 4096, timeout: int | None = None, retries: int = 2,
             actor: Any | None = None,
             credential_store: CredentialStore | None = None,
             policy_store: Any | None = None,
             tool_id: str = "") -> str:
    """按 config 路线调一次 LLM,返回纯文本补全。

    config:ConfigRegistry(读 `ze.llm.*`)。prompt/system:由 mod 拼好的提示词(本层不碰内容)。
    timeout:显式传 int 优先;传 None(缺省)时读 `ze.llm.timeout_sec`(默认 120s)。超时**不重试**
    (见 `_post_json` 经济学注释)。
    """
    _require_model_scope(MODEL_SCOPE_LLM, actor=actor, policy_store=policy_store, tool_id=tool_id)
    eff_timeout = int(timeout) if timeout is not None else int(config.get("ze.llm.timeout_sec", 120) or 120)
    route = str(config.get("ze.llm.route", "claude-cli"))
    model = model or str(config.get("ze.llm.model", "") or "") or _ROUTE_DEFAULT_MODEL.get(route, "")
    if route == "gateway":
        return _with_retry(lambda: _gateway_chat(config, prompt, system, model,
                                                 temperature, max_tokens, eff_timeout,
                                                 actor=actor,
                                                 credential_store=credential_store),
                           retries=retries)
    if route == "claude-cli":
        binary = _resolve_claude_binary(config, actor=actor, credential_store=credential_store)
        return _with_retry(lambda: _cli_chat(
            binary, ["-p", "--model", model],
            prompt, system, eff_timeout), retries=retries)
    if route == "codex-cli":
        # codex exec 在非 git / 非受信目录(如部署目录 /home/amber/deployments/<skill>)会拒跑并报
        # "Not inside a trusted directory and --skip-git-repo-check was not specified"(退出码 1)。
        # 服务态 headless 文本补全不做 git 操作,须显式带 --skip-git-repo-check 才能在任意工作目录运行。
        args = ["exec", "--skip-git-repo-check"]
        binary, acct = _resolve_codex_cli(config, actor=actor, credential_store=credential_store)
        if acct:
            if not _ACCOUNT_RE.fullmatch(acct):   # 配置即作 CLI flag 值,挡异常字符
                raise ZeError("validation_failed", "ze.llm.codex_account 含非法字符",
                              context={})
            args += ["--account", acct]
        if model:
            args += ["-m", model]
        return _with_retry(lambda: _cli_chat(
            binary, args, prompt, system, eff_timeout),
            retries=retries)
    raise ZeError("validation_failed", f"未知 LLM 路线: {route!r}",
                  context={"route": route, "allowed": list(_ROUTE_DEFAULT_MODEL)})


def _gateway_chat(config: Any, prompt: str, system: str | None, model: str,
                  temperature: float, max_tokens: int, timeout: int,
                  *, actor: Any | None = None,
                  credential_store: CredentialStore | None = None) -> str:
    base, key = _resolve_gateway(config, actor=actor, credential_store=credential_store)
    if not base:
        raise ZeError("validation_failed", "ze.llm.gateway_base 未配置", context={})
    if not key:
        raise ZeError("requires_credential",
                      "当前用户未绑定可用网关密钥,且 ze.llm.gateway_key 未配置",
                      context={"provider": PROVIDER_HUPO_GATEWAY, "capability": "llm"})
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}
    data = _post_json(f"{base}/chat/completions", body, timeout,
                      headers={"Authorization": f"Bearer {key}"})
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as e:
        raise ZeError("model_call_failed", f"网关响应结构异常: {e}",
                      context={"keys": list(data) if isinstance(data, dict) else None}) from e


def _cli_chat(binary: str, args: list[str], prompt: str, system: str | None,
              timeout: int) -> str:
    """本地 CLI(claude -p / codex exec):prompt 走 stdin,取 stdout。

    system 直接前置到 prompt(路线无关的最小做法;结构化/系统提示精调留 mod)。
    binary 来自 config(运维信任域);用 list 形式 `subprocess.run`(无 shell,元字符天然失效),
    仅挡空 / NUL / 换行等异常可执行配置。
    """
    if not binary or "\x00" in binary or "\n" in binary:
        raise ZeError("validation_failed", "非法 CLI 可执行配置", context={})
    full = f"{system}\n\n{prompt}" if system else prompt
    try:
        proc = subprocess.run([binary, *args], input=full, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise ZeError("model_call_failed", f"找不到 CLI 可执行: {binary!r}",
                      context={"binary": binary}) from e
    except subprocess.TimeoutExpired as e:
        raise ZeError("model_call_failed", f"CLI 调用超时({timeout}s): {binary!r}",
                      context={"binary": binary}) from e
    if proc.returncode != 0:
        # 取 stderr+stdout 的【尾部】:codex 等 CLI 常把启动 banner(版本/workdir/model…)打在前、
        # 真错误在末尾;旧 [:300] 头部截断只能看到 banner、看不到真因。尾部 1200 字含真错误。
        tail = "\n".join(s for s in ((proc.stderr or "").strip(), (proc.stdout or "").strip()) if s)
        raise ZeError("model_call_failed",
                      f"CLI 退出码 {proc.returncode}: {tail[-1200:]}",
                      context={"binary": binary, "returncode": proc.returncode})
    out = (proc.stdout or "").strip()
    if not out:
        raise ZeError("model_call_failed", f"CLI 无输出: {binary!r}", context={"binary": binary})
    return out


# ==============================================================
# Per-user credential resolution
# ==============================================================

def _actor_user_id(actor: Any | None) -> str:
    return str(getattr(actor, "user_id", "") or "")


def _credential_runtime(actor: Any | None = None,
                        credential_store: CredentialStore | None = None) -> tuple[Any | None, CredentialStore | None]:
    ctx = current_credential_context()
    return actor or ctx.actor, credential_store or ctx.store


def _policy_runtime(
    actor: Any | None = None,
    policy_store: Any | None = None,
    tool_id: str = "",
) -> tuple[Any | None, Any | None, str]:
    ctx = current_credential_context()
    return (
        actor or ctx.actor,
        policy_store or getattr(ctx, "policy_store", None),
        tool_id or str(getattr(ctx, "tool_id", "") or ""),
    )


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _require_model_scope(
    scope: str,
    *,
    actor: Any | None = None,
    policy_store: Any | None = None,
    tool_id: str = "",
) -> None:
    act, policy, tid = _policy_runtime(actor=actor, policy_store=policy_store, tool_id=tool_id)
    uid = _actor_user_id(act)
    if not uid or uid.startswith("__") or policy is None or not tid:
        return
    if not policy.get_model_scope_permission(uid, tid, scope):
        raise ZeError(
            "model_scope_denied",
            f"当前账号没有 {scope} 模型权限",
            context={"user_id": uid, "tool_id": tid, "scope": scope},
        )


def _find_credential(
    *,
    providers: tuple[str, ...],
    actor: Any | None = None,
    credential_store: CredentialStore | None = None,
    capability: str = "llm",
):
    act, store = _credential_runtime(actor, credential_store)
    uid = _actor_user_id(act)
    # demo/访客/系统伪用户("__" 前缀,如 __demo_*)无绑定凭据,且 store.find_for_user→_clean_owner
    # 对 __demo* 硬抛 auth_required。凭据查找只是「可选的每用户覆盖」——查不到该回落 config 缺省
    # (本地 CLI 路线用运维登录、非每用户 ZE 凭据),不该让查找动作本身抛错。与同文件
    # _require_model_scope 的 "__" 访客豁免对齐(否则 demo 终端对话走 codex-cli 必崩在凭据查找门)。
    if not uid or uid.startswith("__") or store is None:
        return None
    return store.find_for_user(uid, providers=providers, capability=capability)


# 自定义端点主机名解析器(P5 DNS rebinding 护栏用):默认 None → socket.getaddrinfo(生产真解析);
# 测试可 monkeypatch 注入(避免对假 host 发真 DNS)。官方/config host 恒放行、不经解析,与此无关。
_AI_HOST_RESOLVER = None


def _guard_gateway_base(base: str, config: Any) -> None:
    """P5:gateway 出站前 SSRF 护栏——用户自填 base_url 若非组织配置的官方网关 host,须解析核验全公网
    (防 DNS rebinding)。官方/config host 放行零成本(只官方路径=零回归)。"""
    from sdk.hub import guard_outbound_host
    host = (urllib.parse.urlsplit(base or "").hostname or "").lower()
    official_host = (urllib.parse.urlsplit(str(config.get("ze.llm.gateway_base", "") or "")).hostname or "").lower()
    try:
        guard_outbound_host(host, official={official_host} if official_host else set(),
                            _resolver=_AI_HOST_RESOLVER)
    except ValueError as e:
        raise ZeError("validation_failed", f"自定义网关端点被 SSRF 防护拒绝: {e}") from e


def _resolve_gateway(
    config: Any,
    *,
    actor: Any | None = None,
    credential_store: CredentialStore | None = None,
) -> tuple[str, str]:
    act, store = _credential_runtime(actor, credential_store)
    uid = _actor_user_id(act)
    rec = None
    # "__" 前缀伪用户(demo/__system__/__mcp__)无每用户绑定凭据;查找会经 _clean_owner 对 __demo* 硬抛
    # auth_required。须跳过查找让其落到下方既有的「__ 豁免」分支(line 下方 `not uid.startswith("__")`
    # 已表意图=__ 用户走全局 gateway key,而非 requires_credential),否则本意图被查找门短路、永远到不了。
    if uid and not uid.startswith("__") and store is not None:
        rec = store.find_for_user(
            uid,
            providers=(PROVIDER_HUPO_GATEWAY,),
            capability="llm",
        )
    if rec is not None and store is not None:
        base = str(rec.metadata.get("base_url") or config.get("ze.llm.gateway_base", "")).rstrip("/")
        key = store.reveal_secret(uid, rec.id)
        store.mark_used(uid, rec.id)
        _guard_gateway_base(base, config)      # P5:用户自填 base 出站前解析核验(官方/config host 放行)
        return base, key
    if uid and not uid.startswith("__"):
        allow_global = _truthy(config.get("ze.llm.allow_global_gateway_key", False))
        if not allow_global:
            raise ZeError(
                "requires_credential",
                "当前用户未绑定组织 AI 网关密钥;请在账号绑定中添加组织 AI 网关",
                context={"provider": PROVIDER_HUPO_GATEWAY, "capability": "llm"},
            )
    return str(config.get("ze.llm.gateway_base", "")).rstrip("/"), str(config.get("ze.llm.gateway_key", "") or "")


def _server_mode() -> bool:
    """ZE_SERVER_MODE(部署/多租户)= 服务器共享凭据 fail-closed 硬门。
    与 image(_resolve_api_key)/copilot(_copilot_engine_for)一致:server_mode 下无个人绑定
    即拒,绝不静默回落服务器共享账号(防陌生人烧公司 codex/claude 订阅)。本地单机(server_mode
    关)行为不变,仍回落 config 默认账号。真值集复用本文件 `_truthy`(含 "on",与
    mcp_bridge._TRUTHY / config KIND_BOOL 同口径)——避免多处裂脑致 ZE_SERVER_MODE=on 时门失效。"""
    return _truthy(os.environ.get("ZE_SERVER_MODE", ""))


def _resolve_codex_cli(
    config: Any,
    *,
    actor: Any | None = None,
    credential_store: CredentialStore | None = None,
) -> tuple[str, str]:
    rec = _find_credential(
        providers=(PROVIDER_CODEX_CLI,),
        actor=actor,
        credential_store=credential_store,
        capability="llm",
    )
    if rec is not None:
        act, store = _credential_runtime(actor, credential_store)
        uid = _actor_user_id(act)
        if store is not None and uid:
            store.mark_used(uid, rec.id)
        # A user binding may select an account, never a host executable.
        binary = str(config.get("ze.llm.codex_bin", "codex"))
        account = str(rec.metadata.get("account") or rec.metadata.get("profile") or config.get("ze.llm.codex_account", "") or "")
        return binary, account
    if _server_mode():
        raise ZeError("requires_credential",
                      "请先在账号里绑定你自己的 Codex 账号(llm 路线用你的订阅额度)",
                      context={"capability": "llm", "provider": PROVIDER_CODEX_CLI})
    return (
        str(config.get("ze.llm.codex_bin", "codex")),
        str(config.get("ze.llm.codex_account", "") or ""),
    )


def _resolve_claude_binary(
    config: Any,
    *,
    actor: Any | None = None,
    credential_store: CredentialStore | None = None,
) -> str:
    rec = _find_credential(
        providers=(PROVIDER_CLAUDE_CLI,),
        actor=actor,
        credential_store=credential_store,
        capability="llm",
    )
    if rec is not None:
        act, store = _credential_runtime(actor, credential_store)
        uid = _actor_user_id(act)
        if store is not None and uid:
            store.mark_used(uid, rec.id)
        return str(config.get("ze.llm.claude_bin", "claude"))
    if _server_mode():
        raise ZeError("requires_credential",
                      "请先在账号里绑定你自己的 Claude 账号(llm 路线用你的订阅额度)",
                      context={"capability": "llm", "provider": PROVIDER_CLAUDE_CLI})
    return str(config.get("ze.llm.claude_bin", "claude"))


# ==============================================================
# 图片(image_call — AI 通道层 3.56.0:按 ze.image.route 分发)
#
# 路线:service(内网共享,现状原样)/ codex-cli(个人 Codex 订阅,原生 image_gen)/
#       openai-api / gemini-api(个人 API key 直调)。返回契约 list[bytes] 恒定,
#       下游调用方零改动。显式选定的路线不可用 = 抛 requires_credential(428),
#       **绝不静默回退**;只有 auto 允许挑可用路线(裁定 1)。
# 计费路线(codex/openai/gemini)retries 强制 0:一次抖动三倍烧配额是事故,不是韧性。
# ==============================================================

def image_call(config: Any, prompt: str, *, model: str | None = None,
               refs: list | None = None, size: str | None = None,
               extra: dict | None = None, timeout: int | None = None,
               retries: int = 2,
               actor: Any | None = None,
               policy_store: Any | None = None,
               tool_id: str = "",
               model_routes: dict | None = None,
               receipt_sink: dict | None = None) -> list[bytes]:
    """按 `ze.image.route` 生图/改图,返回图片字节列表(caller 自行存进 AssetStore)。

    refs 非空 → 改图;每个 ref 可为 bytes(默认 image/png)/(bytes, mime) 元组 /
    字符串(data: / http(s): / 本地路径)。size 例 "1024x1024"(service 路线直控;
    codex 路线仅比例暗示,精确尺寸靠下游裁切;openai 直控;gemini 走 aspect 暗示)。
    extra 透传额外参数(仅 service 路线)。timeout:显式 int 优先,缺省读
    `ze.image.timeout_sec`(默认 600s)。超时**不重试**(见 `_post_json` 经济学注释)。

    model 参数属 service 路线的内网模型名字空间;个人路线各有自己的模型配置
    (`ze.image.codex_model` 等),不消费此参数。

    model_routes(可选,业务层声明的「模型→能产它的路线集合」,如
    {"fal-gpt-image-2": ["service","codex-cli","openai-api"], "gemini3": ["service","gemini-api"]}):
    auto 模式下让「所选模型」决定路由——只在能产该模型的路线里挑第一条可用的,而非固定 provider
    顺序。这样多路线用户选 GPT Image 2 就走 codex/openai(而非误落 gemini 出成 Banana),实现所选=所调。
    缺省/模型无映射 → 回退全路线(行为不变,加性)。
    """
    _require_model_scope(MODEL_SCOPE_IMAGE, actor=actor, policy_store=policy_store, tool_id=tool_id)
    eff_timeout = int(timeout) if timeout is not None else int(config.get("ze.image.timeout_sec", 600) or 600)
    configured = str(config.get("ze.image.route", "service") or "service")
    _kw = dict(model=model, refs=refs, size=size, extra=extra,
               timeout=eff_timeout, retries=retries, actor=actor)
    if configured != "auto":
        # 显式路线(pinned):单路线派发,**绝不 failover**(no-silent-fallback,§3.5 步7)。
        _fill_image_receipt(receipt_sink, configured, model)
        out = _dispatch_image_route(configured, config, prompt, **_kw)
        _set_receipt_trace(receipt_sink, (f"pinned {configured}",))   # 派发 trace 入回执(§3.5 步8)
        return out
    # auto:按 resolver 有序候选逐条尝试,**仅在「连接失败(unreached,provider 未收到→未扣费)」时** failover
    # 落下一条——HTTP 错误/超时/结构异常(可能已扣费)一律不 failover,绝不双重扣费(§P6)。
    ordered = _pick_image_routes_ordered(config, actor=actor, model=model, model_routes=model_routes)
    if not ordered:
        raise ZeError("requires_credential",
                      "还没有可用的出图通道:请先连接一个能出图的 AI 账号(Codex / OpenAI / Gemini)后再试。",
                      context={"capability": "image", "route": "auto",
                               "fix": "connect_image_account",
                               **({"model": model} if model else {})})
    # 派发期解析轨迹(§3.5 步8/§P7 写侧):记录实际候选序 + failover 过程,命中后塞进 receipt_sink,
    # 让消费方按既有 receipt_sink→InvocationReceipt.record 模式把 trace 一并落调用日志(与 route/provider/model 同径)。
    trace: list[str] = [f"auto 候选(按序): {list(ordered)}"]
    last: ZeError | None = None
    for i, route in enumerate(ordered):
        _fill_image_receipt(receipt_sink, route, model)
        try:
            out = _dispatch_image_route(route, config, prompt, **_kw)
            trace.append(f"命中 {route}")
            _set_receipt_trace(receipt_sink, tuple(trace))
            return out
        except ZeError as e:
            last = e
            if (e.context or {}).get("unreached") and i < len(ordered) - 1:
                trace.append(f"{route}: 连接失败(未扣费)→ failover")
                continue                            # 未扣费的连接失败 + 还有候选 → 安全落下一条
            trace.append(f"{route}: {e.code}")
            raise                                   # 其余错误(可能已扣费)或最后一条 → 抛,不 failover
    raise last  # 理论不可达(循环末条已 raise)


def _fill_image_receipt(receipt_sink: dict | None, route: str, model: str | None) -> None:
    """回执出参(ProviderHub §6.1):把**实际落位**的路线/provider/model 回填 caller(记 InvocationReceipt)。
    best-effort、绝不阻断出图;缺省 None 零开销。"""
    if receipt_sink is None:
        return
    try:
        from sdk import hub
        _desc = hub.descriptor_by_route(route)
        receipt_sink["route"] = route
        receipt_sink["provider"] = (_desc.credential_provider if _desc and _desc.credential_provider else route)
        receipt_sink["model_id"] = model or ""
    except Exception:   # noqa: BLE001
        pass


def _set_receipt_trace(receipt_sink: dict | None, trace: tuple) -> None:
    """把派发期解析轨迹写进 receipt_sink(§P7 写侧闭环的 build 半:提供 trace 数据,消费方 record 时带上,
    与 InvocationReceipt.trace 字段对齐)。缺省 None 零开销。"""
    if receipt_sink is not None:
        receipt_sink["trace"] = trace


def _dispatch_image_route(route: str, config: Any, prompt: str, *, model, refs, size, extra,
                          timeout: int, retries: int, actor) -> list[bytes]:
    """按路线字符串派发到对应 _image_via_*(auto failover 与 pinned 共用同一派发,行为一致)。"""
    if route == "service":
        return _image_via_service(config, prompt, model=model, refs=refs, size=size,
                                  extra=extra, timeout=timeout, retries=retries)
    if route == "codex-cli":
        return _image_via_codex(config, prompt, refs=refs, size=size, timeout=timeout, actor=actor)
    if route == "openai-api":
        return _image_via_openai(config, prompt, refs=refs, size=size, timeout=timeout, actor=actor)
    if route == "gemini-api":
        return _image_via_gemini(config, prompt, refs=refs, size=size, timeout=timeout, actor=actor)
    raise ZeError("validation_failed", f"未知生图路线: {route!r}",
                  context={"route": route,
                           "allowed": ["auto", "service", "codex-cli", "openai-api", "gemini-api"]})


def _pick_image_route(config: Any, *, actor: Any | None = None,
                      model: str | None = None,
                      model_routes: dict | None = None,
                      credential_store: Any | None = None) -> str:
    """auto 语义:委托 sdk.hub 唯一解析算法 resolve_image_route(ProviderHub Batch 1)。

    行为与旧私有梯**逐字等价**,已双证:①128 组差分 test_hub_resolve_image_route_differential_
    equivalence(resolve_image_route ≡ 旧固定 provider 顺序 service>openai>gemini>codex);②真 store
    接线 test_projection_matches_find_credential_wiring(project_connections(guarded list_for_user)
    每 provider 可用性 == _find_credential(capability='image'),含 needs_reauth/访客门)。用
    AccessConnection 读投影替 _find_credential 循环——所选=所调、pin 语义、9 错误码收归 hub 一处。

    访客('__' 前缀 / 空 uid / 无 store)→ 无连接(复刻 _find_credential 的访客豁免,避免
    find_for_user 对 __demo* 硬抛)。credential_store 缺省 → ctx.store(与旧 _find_credential(None) 同)。"""
    from sdk import hub
    act, store = _credential_runtime(actor, credential_store)
    uid = _actor_user_id(act)
    if not uid or uid.startswith("__") or store is None:
        conns: tuple = ()
    else:
        conns = hub.project_connections([r.public_dict() for r in store.list_for_user(uid)])
    service_ok = bool(str(config.get("ze.image.service_url", "") or "").strip())
    route = hub.resolve_image_route(
        model, conns, service_configured=service_ok,
        model_routes=model_routes, deploy_audience=hub.AUDIENCE_INTERNAL)
    if route is None:
        raise ZeError("requires_credential",
                      "还没有可用的出图通道:请先连接一个能出图的 AI 账号(Codex / OpenAI / Gemini)后再试。",
                      context={"capability": "image", "route": "auto",
                               "fix": "connect_image_account",
                               **({"model": model} if model else {})})
    return route


def _pick_image_routes_ordered(config: Any, *, actor: Any | None = None,
                               model: str | None = None,
                               model_routes: dict | None = None,
                               credential_store: Any | None = None) -> tuple[str, ...]:
    """P6 auto failover:返回全部可用生图路线(按 ROUTE_PREFERENCE 有序),其首元素 == _pick_image_route。
    连接投影与 _pick_image_route 同源(project_connections(guarded list_for_user)),空 = 无可用
    (caller image_call 抛 requires_credential,同 _pick_image_route)。委托 hub.resolve_image_routes_ordered。"""
    from sdk import hub
    act, store = _credential_runtime(actor, credential_store)
    uid = _actor_user_id(act)
    if not uid or uid.startswith("__") or store is None:
        conns: tuple = ()
    else:
        conns = hub.project_connections([r.public_dict() for r in store.list_for_user(uid)])
    service_ok = bool(str(config.get("ze.image.service_url", "") or "").strip())
    return hub.resolve_image_routes_ordered(
        model, conns, service_configured=service_ok,
        model_routes=model_routes, deploy_audience=hub.AUDIENCE_INTERNAL)


def _image_via_service(config: Any, prompt: str, *, model: str | None,
                       refs: list | None, size: str | None, extra: dict | None,
                       timeout: int, retries: int) -> list[bytes]:
    """内网 keyless 图片服务(AI 通道层前的 image_call 原样搬入,行为逐字节不变)。"""
    base = str(config.get("ze.image.service_url", "")).rstrip("/")
    if not base:
        raise ZeError("validation_failed", "ze.image.service_url 未配置", context={})
    model = model or str(config.get("ze.image.model", "") or "")
    payload: dict[str, Any] = {"model": model, "prompt": prompt}
    if size:
        payload["size"] = size
    if extra:
        payload.update(extra)
    endpoint = "/v1/images/generations"
    if refs:
        endpoint = "/v1/images/edits"
        payload["images"] = [_to_data_uri(r) for r in refs]

    def _do() -> list[bytes]:
        data = _post_json(f"{base}{endpoint}", payload, timeout)
        items = data.get("data") if isinstance(data, dict) else None
        if not items:
            raise ZeError("model_call_failed", "图片服务返回 data 为空",
                          context={"keys": list(data) if isinstance(data, dict) else None})
        out: list[bytes] = []
        for it in items:
            if "b64_json" in it:
                out.append(base64.b64decode(it["b64_json"]))
            elif "url" in it:
                out.append(_download(it["url"], timeout))
        if not out:
            raise ZeError("model_call_failed", "图片服务返回项无 b64_json / url",
                          context={"item_keys": [list(it.keys()) for it in items
                                                 if isinstance(it, dict)]})
        return out

    return _with_retry(_do, retries=retries)


# ==============================================================
# 个人生图路线(AI 通道层 3.56.0)
# ==============================================================

# 同一 CODEX_HOME 上的 codex 子进程全局互斥:共享 home 的 OAuth refresh token 轮换会让
# 生图与 copilot 互相踢下线(对抗审查致命点4)——不只是生图侧 cap=1。锁键=home 路径
# (共享 home 才互踢;不同用户不同 home 天然并行)。"" = 宿主默认 home 域。
# ze.image.codex_max_concurrency>1 暂不放开(跨功能安全优先,阶段0实测后再议)。
_USER_CLI_LOCKS: dict[str, threading.Lock] = {}
_USER_CLI_LOCKS_GUARD = threading.Lock()


def _user_cli_lock(home_key: str) -> threading.Lock:
    with _USER_CLI_LOCKS_GUARD:
        lock = _USER_CLI_LOCKS.get(home_key or "__host__")
        if lock is None:
            lock = _USER_CLI_LOCKS[home_key or "__host__"] = threading.Lock()
        return lock


# codex CLI 落盘契约是版本特有的(实证过的版本才放行)。允许范围外 = 硬禁路线,
# 绝不静默。0.125=用户本机实证(2026-07-17),0.134=amber 阶段0实证(2026-07-18,
# 1024×1536 真图+provenance 事件在场+workspace-write 沙箱直通),0.144=vps-c1 实证
# (2026-07-22,同款密封 env+参数真跑:out.png 按指令落地+generated_images/ 照旧写入
# +事件流 image_gen 标识在场——此前"0.140+ 改 base64 进 rollout"的推断在 0.144.6 不成立;
# 0.140~0.143 无实证仍禁)。
_CODEX_IMAGE_CLI_ALLOWED_PREFIXES = ("0.125.", "0.134.", "0.144.")

# provenance 校验的事件流特征:--json 事件流里原生图像工具调用的标识子串(阶段0按实机
# 事件格式校准)。退化写代码画图的会话不含这些标识。
_CODEX_IMAGE_TOOL_MARKERS = ("image_gen", "image_generation")

_OPENAI_IMAGE_MODEL = "gpt-image-1"          # 直调默认档(gpt-image-2 需更高组织验证)
_GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"


def _codex_exec(binary: str, args: list[str], prompt: str, *, timeout: int,
                env: dict[str, str], cwd: str | None = None) -> "subprocess.CompletedProcess[str]":
    """codex exec 子进程唯一执行原语:stdin 堵漏 + 进程组 kill + 错误包装。

    参数拼装归 caller(schema/生图/文本各有契约);本层只负责「跑起来、跑不完杀干净」。
    start_new_session=True 起独立进程组,超时 killpg 连孙进程一起收(codex 会再起子进程)。
    """
    try:
        proc = subprocess.Popen([binary, *args], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, cwd=cwd, start_new_session=True)
    except FileNotFoundError as e:
        raise ZeError("model_call_failed", f"找不到 CLI 可执行: {binary!r}",
                      context={"binary": binary}) from e
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()
        raise ZeError("model_call_timeout", f"codex exec 超时({timeout}s),进程组已终止",
                      context={"binary": binary, "timeout": timeout}) from e
    return subprocess.CompletedProcess([binary, *args], proc.returncode, stdout, stderr)


def _codex_cli_version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             timeout=20).stdout or ""
    except (OSError, subprocess.SubprocessError) as e:
        raise ZeError("model_call_failed", f"codex --version 探测失败: {e}",
                      context={"binary": binary}) from e
    m = re.search(r"(\d+\.\d+\.\d+)", out)
    return m.group(1) if m else out.strip()


def _sealed_cli_env(home_env: dict[str, str]) -> dict[str, str]:
    """白名单构建子进程 env——绝不 os.environ.copy()。

    env 里存在 OPENAI_API_KEY 时 codex 会**静默切成 API 计费**(订阅路线变按张付费,
    对抗审查致命点2);白名单从根上排除,并有单测断言。
    """
    keep = ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update(home_env)
    env.pop("OPENAI_API_KEY", None)
    return env


def _image_via_codex(config: Any, prompt: str, *, refs: list | None,
                     size: str | None, timeout: int,
                     actor: Any | None) -> list[bytes]:
    """codex 原生 image_gen(gpt-image-2 底座,ChatGPT 订阅额度)。附录 A 契约逐条。"""
    rec = _find_credential(providers=(PROVIDER_CODEX_CLI,), actor=actor,
                           credential_store=None, capability="image")
    if rec is None:
        raise ZeError("requires_credential",
                      "codex-cli 生图路线需要绑定个人 Codex 账号(不回退到共享登录)",
                      context={"provider": PROVIDER_CODEX_CLI, "capability": "image"})
    home = str(rec.metadata.get(META_HOME_DIR) or "")
    if not home:
        raise ZeError("requires_credential", "codex 绑定缺少隔离 home(请重新绑定)",
                      context={"provider": PROVIDER_CODEX_CLI, "capability": "image"})
    binary = str(config.get("ze.llm.codex_bin", "codex"))
    version = _codex_cli_version(binary)
    if not version.startswith(_CODEX_IMAGE_CLI_ALLOWED_PREFIXES):
        raise ZeError("model_call_failed",
                      f"codex CLI {version} 不在生图路线允许范围 {_CODEX_IMAGE_CLI_ALLOWED_PREFIXES}"
                      "(落盘契约版本特有,升级需重新验证后放开)",
                      context={"binary": binary, "version": version})
    act, store = _credential_runtime(actor, None)
    uid = _actor_user_id(act) or ""
    if store is not None and uid:
        store.mark_used(uid, rec.id)
    model = str(config.get("ze.image.codex_model", "gpt-5.5") or "gpt-5.5")

    with _user_cli_lock(home):
        with tempfile.TemporaryDirectory(prefix="ze-image-codex-") as tmpdir:
            tmp = Path(tmpdir)
            out_png = tmp / "out.png"
            ref_paths: list[Path] = []
            for i, r in enumerate(refs or []):
                data = r[0] if isinstance(r, tuple) else r
                if isinstance(data, str):
                    data = _ref_to_bytes(data, timeout)
                p = tmp / f"ref_{i}.png"
                p.write_bytes(data)
                ref_paths.append(p)
            hint = ""
            if size and "x" in str(size):
                w, h = str(size).split("x", 1)
                hint = (f"\n生成最接近 {w}:{h} 比例、且不小于 {size} 的图;"
                        "精确尺寸由下游裁切保证。")
            full_prompt = (
                f"{prompt}\n---\n"
                "必须用你内置的原生图像生成工具(image_gen)出图。禁止写任何 PIL/matplotlib/"
                "Pillow/numpy/SVG/canvas 代码来\"画\"图——发现写绘图代码即视为失败。\n"
                f"生成后把最终 PNG 复制到:{out_png}。只输出这一张图。{hint}"
            )
            args = ["exec", "--skip-git-repo-check", "--color", "never", "--json",
                    "--sandbox", "workspace-write",
                    "-C", str(tmp),
                    "-c", "skills.enabled=false",
                    "-c", "service_tier=fast",
                    "-c", "model_reasoning_effort=low",
                    "-c", "features.image_generation=true",
                    # 宿主 config 里的 MCP servers(如 figma)在 headless 下发 OAuth 噪声/偶发卡顿,
                    # 生图不需要任何 MCP —— 显式清空(阶段0 amber stderr 实证 rmcp AuthRequired)。
                    "-c", "mcp_servers={}",
                    # 原生工具先落 $CODEX_HOME/generated_images 再 cp——home 须可写
                    "-c", f'sandbox_workspace_write.writable_roots=["{home}"]',
                    "-m", model]
            for p in ref_paths:
                args += ["-i", str(p)]
            args.append("-")
            started = time.time()
            proc = _codex_exec(binary, args, full_prompt, timeout=timeout,
                               env=_sealed_cli_env(cli_home_env(PROVIDER_CODEX_CLI, home)),
                               cwd=str(tmp))
            if proc.returncode != 0:
                raise ZeError("model_call_failed",
                              f"codex 生图退出码 {proc.returncode}: "
                              f"{(proc.stderr or '').strip()[:400] or (proc.stdout or '').strip()[:300]}",
                              context={"returncode": proc.returncode})
            # provenance:事件流必须真调过原生图像工具——退化写代码产出的是**合法 PNG 假图**
            # (魔数/解码/尺寸三关全过),只能校验来源而非产物(对抗审查致命点1)。
            if not any(marker in (proc.stdout or "") for marker in _CODEX_IMAGE_TOOL_MARKERS):
                raise ZeError("model_call_failed",
                              "codex 会话未调用原生图像生成工具(疑似退化为写代码画图),拒绝采信产物",
                              context={"provenance": "image_generation tool not observed"})
            # 输出捕获降级阶梯:①指定路径 ②临时目录新 png ③home generated_images 新 png
            candidates: list[Path] = []
            if out_png.is_file():
                candidates.append(out_png)
            candidates += [p for p in sorted(tmp.glob("*.png"), key=lambda x: x.stat().st_mtime,
                                             reverse=True) if p not in ref_paths and p != out_png]
            gen_dir = Path(home) / "generated_images"
            if gen_dir.is_dir():
                candidates += sorted((p for p in gen_dir.rglob("*.png")
                                      if p.stat().st_mtime >= started - 1),
                                     key=lambda x: x.stat().st_mtime, reverse=True)
            for cand in candidates:
                data = cand.read_bytes()
                if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 4096:
                    return [data]
            raise ZeError("model_call_failed", "codex 生图会话结束但未捕获到有效 PNG 产物",
                          context={"searched": [str(tmp), str(gen_dir)]})


def _resolve_api_key(provider: str, *, actor: Any | None, capability: str = "image") -> str:
    rec = _find_credential(providers=(provider,), actor=actor,
                           credential_store=None, capability=capability)
    if rec is None:
        raise ZeError("requires_credential",
                      f"{provider} 路线需要绑定个人 API key(显式路线不回退)",
                      context={"provider": provider, "capability": capability})
    act, store = _credential_runtime(actor, None)
    uid = _actor_user_id(act)
    secret = ""
    if store is not None and uid:
        secret = store.reveal_secret(uid, rec.id) or ""
        store.mark_used(uid, rec.id)
    if not secret:
        raise ZeError("requires_credential", f"{provider} 绑定缺少可用 key(请重新绑定)",
                      context={"provider": provider, "capability": capability})
    return secret


def _image_via_openai(config: Any, prompt: str, *, refs: list | None,
                      size: str | None, timeout: int,
                      actor: Any | None) -> list[bytes]:
    """OpenAI Images API 直调(按张计费 → retries=0;size 直控;b64 直返)。"""
    key = _resolve_api_key(PROVIDER_OPENAI_API, actor=actor)
    headers = {"Authorization": f"Bearer {key}"}
    if refs:
        fields = [("model", _OPENAI_IMAGE_MODEL), ("prompt", prompt)]
        if size:
            fields.append(("size", str(size)))
        files = []
        for i, r in enumerate(refs):
            data = r[0] if isinstance(r, tuple) else r
            if isinstance(data, str):
                data = _ref_to_bytes(data, timeout)
            files.append(("image[]", f"ref_{i}.png", "image/png", data))
        data = _post_multipart("https://api.openai.com/v1/images/edits",
                               fields, files, timeout, headers=headers)
    else:
        body: dict[str, Any] = {"model": _OPENAI_IMAGE_MODEL, "prompt": prompt}
        if size:
            body["size"] = str(size)
        data = _post_json("https://api.openai.com/v1/images/generations",
                          body, timeout, headers=headers)
    items = data.get("data") if isinstance(data, dict) else None
    out = [base64.b64decode(it["b64_json"]) for it in (items or [])
           if isinstance(it, dict) and it.get("b64_json")]
    if not out:
        raise ZeError("model_call_failed", "OpenAI Images 返回无 b64_json 产物",
                      context={"keys": list(data) if isinstance(data, dict) else None})
    return out


def _image_via_gemini(config: Any, prompt: str, *, refs: list | None,
                      size: str | None, timeout: int,
                      actor: Any | None) -> list[bytes]:
    """Gemini generateContent 直调(免费额度 ~500/天;inline_data 带参考图)。"""
    key = _resolve_api_key(PROVIDER_GEMINI_API, actor=actor)
    parts: list[dict[str, Any]] = []
    for r in refs or []:
        data = r[0] if isinstance(r, tuple) else r
        if isinstance(data, str):
            data = _ref_to_bytes(data, timeout)
        parts.append({"inline_data": {"mime_type": "image/png",
                                      "data": base64.b64encode(data).decode("ascii")}})
    hint = f"\n(生成最接近 {size} 尺寸/比例的图)" if size else ""
    parts.append({"text": prompt + hint})
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{_GEMINI_IMAGE_MODEL}:generateContent?key={urllib.parse.quote(key)}")
    data = _post_json(url, {"contents": [{"parts": parts}]}, timeout)
    out: list[bytes] = []
    for cand in (data.get("candidates") or []) if isinstance(data, dict) else []:
        for part in ((cand.get("content") or {}).get("parts") or []):
            blob = (part.get("inlineData") or part.get("inline_data") or {})
            if blob.get("data"):
                out.append(base64.b64decode(blob["data"]))
    if not out:
        raise ZeError("model_call_failed", "Gemini 返回无图像产物",
                      context={"keys": list(data) if isinstance(data, dict) else None})
    return out



def _guarded_open(req, *, timeout):
    """出站统一入口:跟随重定向,但每一跳都过 SSRF 护栏。

    刻意不直接调 urllib.request.urlopen —— 它默认静默跟随 302,登记期/发请求前的两道护栏
    对重定向后的地址一次都问不到(实测:合规 host 回 Location: http://169.254.169.254/… 会被跟进)。
    """
    from sdk.hub import guarded_opener
    return guarded_opener().open(req, timeout=timeout)

def gemini_validate_key(key: str, *, timeout: int = 20) -> None:
    """绑定即验证(裁定 5 / 拍板 7):models.list 最小校验,坏 key 绑不进去;零生图消耗。"""
    url = ("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1&key="
           + urllib.parse.quote(key or ""))
    req = urllib.request.Request(url, method="GET")
    try:
        with _guarded_open(req, timeout=timeout) as resp:
            json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ZeError("validation_failed",
                      f"Gemini key 校验失败(HTTP {e.code}):key 无效或未开通",
                      context={"status": e.code}) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise ZeError("model_call_failed", f"Gemini key 校验网络失败: {e}",
                      context={}) from e


def _ref_to_bytes(ref: str, timeout: int) -> bytes:
    if ref.startswith("data:"):
        return base64.b64decode(ref.split(",", 1)[1])
    if ref.startswith(("http://", "https://")):
        return _download(ref, timeout)
    return Path(ref).read_bytes()


# ==============================================================
# I/O helpers(stdlib only)
# ==============================================================

def _post_multipart(url: str, fields: list, files: list, timeout: int,
                    headers: dict | None = None) -> dict:
    """multipart/form-data POST(stdlib 手搓;OpenAI images/edits 用)。

    fields=[(name, value)];files=[(name, filename, mime, bytes)]。错误语义同 _post_json。
    """
    boundary = "----ze-" + base64.urlsafe_b64encode(os.urandom(12)).decode("ascii")
    parts: list[bytes] = []
    for name, value in fields:
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                      f"name=\"{name}\"\r\n\r\n{value}\r\n").encode("utf-8"))
    for name, filename, mime, data in files:
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                      f"name=\"{name}\"; filename=\"{filename}\"\r\n"
                      f"Content-Type: {mime}\r\n\r\n").encode("utf-8") + data + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode("ascii")
    hdr = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdr, method="POST")
    try:
        with _guarded_open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise ZeError("model_call_failed", f"HTTP {e.code}: {detail}",
                      context={"status": e.code, "url": url}) from e
    except TimeoutError as e:
        raise ZeError("model_call_timeout", f"请求超时({timeout}s)",
                      context={"url": url, "timeout": timeout}) from e
    except urllib.error.URLError as e:
        # 连接阶段失败 → provider 未收到 → 未扣费;打 unreached=True 供 P6 安全 failover(同 _post_json)。
        raise ZeError("model_call_failed", f"连接失败: {e.reason}",
                      context={"url": url, "unreached": True}) from e


def _post_json(url: str, body: dict, timeout: int,
               headers: dict | None = None) -> dict:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, data=raw, headers=hdr, method="POST")
    try:
        with _guarded_open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise ZeError("model_call_failed", f"HTTP {e.code}: {detail}",
                      context={"status": e.code, "url": url}) from e
    except TimeoutError as e:
        # 读响应体阶段超时抛的是**裸 TimeoutError**(socket.timeout 自 Py3.10 即其别名),不是
        # URLError——若不在此显式接住,它会冒泡到 _with_retry 的通用 `except Exception` 被当瞬时
        # I/O 重试。而生图/LLM 调用是分钟级 + 按次计费:超时后盲目重试 = 每次再等满一个 timeout +
        # 再付一次费(事故复盘 retries=2 → 单击 3×timeout≈9min、三倍成本),且真超时通常下次还会超。
        # 故包成 ZeError(_with_retry 的 `except ZeError: raise` 使其**不重试**),交回上层由用户决定
        # 加大 ze.image.timeout_sec / ze.llm.timeout_sec 或换参。连接阶段超时走下方 URLError 分支,
        # 同样落 ZeError 不重试,语义一致。
        raise ZeError("model_call_timeout", f"请求超时({timeout}s)",
                      context={"url": url, "timeout": timeout}) from e
    except urllib.error.URLError as e:
        # 连接阶段失败(DNS/拒连/无响应):provider **从未收到请求 → 必然未扣费**。打正向标记
        # unreached=True,让 auto 模式 failover 能**安全**落下一路线(§P6:只有确定未扣费才 failover;
        # HTTPError/超时/结构异常均不带此标记 → 不 failover,绝不双重扣费)。
        raise ZeError("model_call_failed", f"连接失败: {e.reason}",
                      context={"url": url, "unreached": True}) from e


def _download(url: str, timeout: int) -> bytes:
    """下图;失败包成 ZeError(**不**把可能带签名 token 的 url 写进消息/上下文)。"""
    req = urllib.request.Request(url, headers={"User-Agent": "ze-skill-build/1.0"})
    try:
        with _guarded_open(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as e:
        raise ZeError("model_call_failed", "下载结果图失败",
                      context={"reason": type(e).__name__}) from e


def _to_data_uri(ref) -> str:
    """ref → data URI。只收 bytes / (bytes,mime) / data:|http(s): 串。

    **不**接受裸本地路径(否则在服务端会成任意文件读原语);mod 要传本地图自行读成 bytes。
    """
    if isinstance(ref, tuple) and len(ref) == 2:                 # (bytes, mime)
        blob, mime = ref
        return f"data:{mime};base64,{base64.b64encode(bytes(blob)).decode()}"
    if isinstance(ref, (bytes, bytearray)):                      # 裸 bytes → 默认 png
        return f"data:image/png;base64,{base64.b64encode(bytes(ref)).decode()}"
    s = str(ref)
    if s.startswith("data:") or s.startswith("http://") or s.startswith("https://"):
        return s
    raise ZeError("validation_failed",
                  "image refs 只收 bytes / (bytes,mime) / data:|http(s): 串(本地图请先读成 bytes)",
                  context={"got": type(ref).__name__})




# ==============================================================
# llm_schema_call — 本地 CLI 结构化输出(codex / claude)编排(F13 回收)
#
# 1.100.0 复议记录:core 1.90 把 `llm_vision_schema_call` 下沉 mod 时只有 audit-v3 一个
# 消费者;此后 object-library-v3(objlib/vision.py)、playable-scene-engine-v3(llm_call.py)
# 各自原样拷贝,三份 byte-similar=共痛达晋级线,收回 sdk。红线不破:本函数仍只做**机制**
# (CLI 参数拼装/子进程/超时/JSON 解析);schema 内容、prompt、模型选择、双语策略全部由
# mod 传参。收编蓝本=playable 拷贝件(最新,含 skills.enabled=false crash 修复);
# 增量修复:claude 分支文本-only 且 cwd 缺省时不再 IndexError(fallback Path.cwd())。
# ==============================================================


def _scrape_structured_json(text: str) -> dict[str, Any] | None:
    """CLI stdout → dict(容忍前后噪声:取首 { 到尾 } 再 parse)。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None


def llm_schema_call(*, engine: str, binary: str, prompt: str,
                    output_schema: dict[str, Any],
                    image_paths: list[str | Path] | tuple[()] = (),
                    video_paths: list[str | Path] | tuple[()] = (),
                    model: str = "", timeout: int = 180, api_key: str = "",
                    cwd: str | Path | None = None,
                    env: dict[str, str] | None = None,
                    reasoning_effort: str = "high") -> dict[str, Any]:
    """调用本地 CLI(codex exec / claude -p)做结构化输出,返回 JSON dict。

    机制层:把 prompt / schema /(可选)图片或视频路径交给 CLI 并解析结构化 JSON。
    视频必须位于 caller 指定的临时 ``cwd`` 内；Codex 在这类调用中获得
    workspace-write 沙箱，Claude 则放行受限 Bash(ffprobe/ffmpeg)，以便自行抽帧。
    """
    engine = str(engine or "codex").lower()
    if engine not in {"codex", "claude"}:
        raise ZeError("validation_failed", f"未知 LLM 引擎: {engine!r}",
                      context={"allowed": ["codex", "claude"]})
    if not binary or "\x00" in binary or "\n" in binary:
        raise ZeError("validation_failed", "非法 CLI 可执行配置", context={})
    images = [Path(p).expanduser().resolve() for p in image_paths]
    missing = [str(p) for p in images if not p.is_file()]
    if missing:
        raise ZeError("validation_failed", "图片不存在", context={"missing": missing[:5]})
    videos = [Path(p).expanduser().resolve() for p in video_paths]
    missing_videos = [str(p) for p in videos if not p.is_file()]
    if missing_videos:
        raise ZeError("validation_failed", "视频不存在",
                      context={"missing": missing_videos[:5]})
    base_cwd = Path(cwd).expanduser().resolve() if cwd is not None else None
    if videos:
        if engine not in {"codex", "claude"}:
            raise ZeError("validation_failed", "当前只有 Codex / Claude 支持自行读取视频",
                          context={"engine": engine})
        if base_cwd is None or any(
            path != base_cwd and base_cwd not in path.parents for path in videos
        ):
            raise ZeError(
                "validation_failed",
                "视频必须位于本次调用的临时工作目录内",
                context={"cwd": str(base_cwd or "")},
            )
    if engine == "claude":
        return _claude_schema_call(
            binary, prompt, images, videos, output_schema, model, timeout,
            base_cwd, env,
        )
    return _codex_schema_call(
        binary, prompt, images, videos, output_schema, model, timeout,
        api_key, base_cwd, reasoning_effort, env,
    )


def _codex_schema_call(binary: str, prompt: str, image_paths: list[Path],
                       video_paths: list[Path],
                       output_schema: dict[str, Any], model: str, timeout: int,
                       api_key: str, cwd: Path | None,
                       reasoning_effort: str,
                       caller_env: dict[str, str] | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ze-llm-schema-") as tmpdir:
        tmp = Path(tmpdir)
        schema_path = tmp / "schema.json"
        out_path = tmp / "answer.txt"
        schema_path.write_text(json.dumps(output_schema, ensure_ascii=False), encoding="utf-8")
        full_prompt = prompt
        if video_paths:
            full_prompt += (
                "\n\n本次调用的临时工作目录内有以下视频：\n"
                + "\n".join(f"- {path.name}" for path in video_paths)
                + "\n请实际使用 ffprobe 检查视频，并按任务需要自行使用 ffmpeg 抽帧、"
                  "查看画面；不要仅根据文件名或文字猜测。"
                  "只可在当前临时工作目录写入抽帧文件，不要修改输入视频。"
            )
        args = [
            binary, "exec",
            "--skip-git-repo-check", "--ephemeral", "--ignore-rules", "--ignore-user-config",
            # 宿主 ~/.claude/skills 里任何缺 frontmatter 的 SKILL.md 会让 codex session 直接退出;
            # 结构化调用不需要宿主 skills,显式关掉。
            "-c", "skills.enabled=false",
            "--sandbox", "workspace-write" if video_paths else "read-only",
            "--color", "never",
            "--output-schema", str(schema_path),
            "--output-last-message", str(out_path),
        ]
        if model:
            args += ["-m", model]
        if reasoning_effort:
            args += ["-c", f"model_reasoning_effort={reasoning_effort}"]
        args += ["-C", str(cwd or tmp)]
        for image_path in image_paths:
            args += ["-i", str(image_path)]
        args.append("-")
        # 3.56:走 _codex_exec 原语(进程组 kill 统一);env 仍白名单构建。schema 调用是
        # **API 计费语义**(caller 显式传 key)——api_key 在此显式注入是本调用的预期计费方式,
        # 与订阅生图路线(_sealed_cli_env 剔 key)语义相反,勿混用。
        env = _sealed_cli_env(caller_env or {})
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        proc = _codex_exec(
            binary,
            args[1:],
            full_prompt,
            timeout=timeout,
            env=env,
            cwd=str(cwd or tmp),
        )
        if proc.returncode != 0:
            # Prefer the end: session banners live at the start; real ERROR lines
            # (usage limit / auth / tool failures) are at the end of stderr/stdout.
            detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
            raise ZeError("model_call_failed",
                          f"codex exec 退出码 {proc.returncode}: {detail[-600:]}",
                          context={"binary": binary, "returncode": proc.returncode})
        raw = out_path.read_text(encoding="utf-8") if out_path.exists() else (proc.stdout or "")
        payload = _scrape_structured_json(raw)
        if payload is None:
            raise ZeError("model_call_failed", f"codex output is not JSON: {raw[:200]}",
                          context={"binary": binary})
        return payload


# Claude Code --allowedTools 语法(官方帮助例: "Bash(git *) Edit")；仅视频路径放开
# ffprobe/ffmpeg，其余仍只读 Read。
_CLAUDE_VIDEO_ALLOWED_TOOLS = "Read Bash(ffprobe *) Bash(ffmpeg *)"


def _claude_run(args: list[str], *, timeout: int, cwd: str,
                env: dict[str, str]) -> "subprocess.CompletedProcess[str]":
    """claude -p 子进程执行原语:进程组 kill，避免视频路径下 ffmpeg 孙进程残留。"""
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=cwd, start_new_session=True,
        )
    except FileNotFoundError as e:
        raise ZeError("model_call_failed", f"找不到 CLI 可执行: {args[0]!r}",
                      context={"binary": args[0]}) from e
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()
        raise ZeError("model_call_timeout", f"claude -p 调用超时({timeout}s),进程组已终止",
                      context={"binary": args[0], "timeout": timeout}) from e
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def _claude_schema_call(binary: str, prompt: str, image_paths: list[Path],
                        video_paths: list[Path],
                        output_schema: dict[str, Any], model: str,
                        timeout: int, cwd: Path | None,
                        caller_env: dict[str, str] | None = None) -> dict[str, Any]:
    # 文本-only 且未指定 cwd → 落当前工作目录(蓝本在此处 image_paths[0] 会 IndexError)
    base = cwd or (image_paths[0].parent if image_paths else Path.cwd())
    allowed_dirs = sorted({
        str(base.resolve()),
        *(str(p.parent) for p in image_paths),
        *(str(p.parent) for p in video_paths),
    })
    full_prompt = prompt
    if video_paths:
        full_prompt += (
            "\n\n本次调用的临时工作目录内有以下视频：\n"
            + "\n".join(f"- {path.name}" for path in video_paths)
            + "\n请实际使用 ffprobe 检查视频，并按任务需要自行使用 ffmpeg 抽帧、"
              "查看画面；不要仅根据文件名或文字猜测。"
              "只可在当前临时工作目录写入抽帧文件，不要修改输入视频。"
        )
    args = [binary, "-p", full_prompt]
    for directory in allowed_dirs:
        args += ["--add-dir", directory]
    args += [
        "--allowedTools",
        _CLAUDE_VIDEO_ALLOWED_TOOLS if video_paths else "Read",
        "--output-format", "json",
        "--json-schema", json.dumps(output_schema, ensure_ascii=False),
        "--no-session-persistence",
    ]
    if model:
        args += ["--model", model]
    proc = _claude_run(
        args, timeout=timeout, cwd=str(base), env=_sealed_cli_env(caller_env or {}),
    )
    if proc.returncode != 0:
        raise ZeError("model_call_failed",
                      f"claude -p 退出码 {proc.returncode}: {(proc.stderr or '').strip()[:600] or (proc.stdout or '').strip()[:400]}",
                      context={"binary": binary, "returncode": proc.returncode})
    envelope = _scrape_structured_json(proc.stdout or "")
    if envelope is None:
        raise ZeError("model_call_failed", f"claude output is not JSON: {(proc.stdout or '')[:200]}",
                      context={"binary": binary})
    if envelope.get("is_error"):
        raise ZeError("model_call_failed", f"claude error: {str(envelope.get('result'))[:400]}",
                      context={"binary": binary})
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    fallback = _scrape_structured_json(str(envelope.get("result") or ""))
    if fallback is None:
        raise ZeError("model_call_failed", "claude returned no structured output",
                      context={"binary": binary})
    return fallback


def _bound_user_credential(
    provider: str,
    *,
    actor: Any | None = None,
    credential_store: CredentialStore | None = None,
):
    """Resolve one active credential for the current ZE user, never ambient state."""
    rec = _find_credential(
        providers=(provider,),
        actor=actor,
        credential_store=credential_store,
        capability="llm",
    )
    if rec is None:
        raise ZeError(
            "requires_credential",
            "当前用户未绑定所选 AI 账号",
            context={"provider": provider, "capability": "llm"},
        )
    act, store = _credential_runtime(actor, credential_store)
    uid = _actor_user_id(act)
    if store is None or not uid:
        raise ZeError(
            "requires_credential",
            "当前调用缺少用户凭据上下文",
            context={"provider": provider, "capability": "llm"},
        )
    return uid, store, rec


def _cursor_sdk_schema_call(
    *,
    prompt: str,
    output_schema: dict[str, Any],
    api_key: str,
    model: str,
    image_paths: list[Path],
    video_paths: list[Path] | tuple[()] = (),
    timeout: int = 180,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """One isolated Cursor local-agent run with timeout/cancel/cleanup.

    Uses the installed Python bridge (`cursor-sdk==1.0.26`) lifecycle:
    ``Agent.create → send → wait``, and cancels via ``Run.cancel`` /
    ``Agent.cancel_run`` when the caller times out or asks to abort.

    Images and videos are copied into the isolated workspace; the agent is told
    their filenames so it can inspect them with local tools (ffprobe/ffmpeg).
    """
    def safe_error(value: Any) -> str:
        text = str(value)
        return text.replace(api_key, "[REDACTED]") if api_key else text

    try:
        from cursor_sdk import (  # type: ignore
            Agent, AgentOptions, LocalAgentOptions, SandboxOptions,
        )
    except ImportError as exc:
        raise ZeError(
            "dependency_unavailable",
            "Cursor SDK 未安装;请准备 python-runtime 依赖环境"
            "（requirements: cursor-sdk==1.0.26）",
            context={"dependency": "cursor-sdk", "required": "1.0.26"},
        ) from exc

    if not model:
        raise ZeError("validation_failed", "Cursor 本地运行必须选择模型", context={})
    timeout = max(1, int(timeout or 180))
    with tempfile.TemporaryDirectory(prefix="ze-cursor-schema-") as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir(mode=0o700)
        store_dir = Path(tmpdir) / "store"
        store_dir.mkdir(mode=0o700)
        copied_images: list[Path] = []
        for idx, source in enumerate(image_paths):
            suffix = source.suffix.lower() or ".png"
            target = workspace / f"reference-{idx}{suffix}"
            target.write_bytes(source.read_bytes())
            copied_images.append(target)
        copied_videos: list[Path] = []
        for idx, source in enumerate(video_paths):
            name = source.name or f"optimizer-video-{idx + 1}.mp4"
            target = workspace / name
            if target.exists():
                suffix = source.suffix.lower() or ".mp4"
                target = workspace / f"optimizer-video-{idx + 1}{suffix}"
            target.write_bytes(source.read_bytes())
            copied_videos.append(target)
        schema_path = workspace / "output-schema.json"
        schema_path.write_text(json.dumps(output_schema, ensure_ascii=False), encoding="utf-8")
        full_prompt = (
            f"{prompt}\n\n"
            "Return exactly one JSON object that conforms to output-schema.json. "
            "Do not edit files and do not wrap the JSON in Markdown."
        )
        if copied_videos:
            full_prompt += (
                "\nVideos in this isolated workspace "
                "(inspect with ffprobe/ffmpeg; do not invent motion absent from the file):\n"
                + "\n".join(path.name for path in copied_videos)
            )
        if copied_images:
            full_prompt += "\nReference images in this isolated workspace:\n" + "\n".join(
                path.name for path in copied_images
            )

        agent = None
        run = None
        result_box: dict[str, Any] = {}
        error_box: dict[str, BaseException] = {}

        def _cancel_active() -> None:
            if run is not None:
                try:
                    run.cancel()
                except Exception:
                    run_id = str(getattr(run, "id", "") or "")
                    agent_id = str(getattr(agent, "agent_id", "") or "")
                    if run_id:
                        try:
                            Agent.cancel_run(run_id, agent_id=agent_id or None)
                        except Exception:
                            pass

        try:
            agent = Agent.create(
                AgentOptions(
                    api_key=api_key,
                    model=model,
                    local=LocalAgentOptions(
                        cwd=str(workspace),
                        setting_sources=(),
                        sandbox_options=SandboxOptions(enabled=True),
                        # cursor-sdk 1.0.26 accepts sqlite/jsonl/custom only;
                        # "local" is rejected by the bridge as unsupported.
                        store={"type": "sqlite", "root_dir": str(store_dir)},
                    ),
                )
            )
            run = agent.send(full_prompt)

            def waiter() -> None:
                try:
                    result_box["result"] = run.wait()
                except BaseException as exc:  # noqa: BLE001 — surface to caller thread
                    error_box["error"] = exc

            worker = threading.Thread(target=waiter, name="ze-cursor-wait", daemon=True)
            worker.start()
            deadline = time.monotonic() + timeout
            while worker.is_alive():
                if cancel_event is not None and cancel_event.is_set():
                    _cancel_active()
                    worker.join(timeout=5)
                    raise ZeError(
                        "cancelled",
                        "Cursor Agent 已取消",
                        context={"provider": PROVIDER_CURSOR_API,
                                 "run_id": str(getattr(run, "id", ""))},
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _cancel_active()
                    worker.join(timeout=5)
                    raise ZeError(
                        "model_call_timeout",
                        f"Cursor Agent 超时({timeout}s)，已请求取消",
                        context={"provider": PROVIDER_CURSOR_API, "timeout": timeout,
                                 "run_id": str(getattr(run, "id", ""))},
                    )
                worker.join(timeout=min(0.5, remaining))

            if "error" in error_box:
                raise error_box["error"]
            result = result_box.get("result")
            if result is None:
                raise ZeError(
                    "model_call_failed",
                    "Cursor Agent 未返回结果",
                    context={"provider": PROVIDER_CURSOR_API},
                )
        except ZeError:
            raise
        except Exception as exc:  # SDK transport/runtime error; never include api_key
            raise ZeError(
                "model_call_failed",
                f"Cursor Agent 调用失败: {type(exc).__name__}: {safe_error(exc)[:400]}",
                context={"provider": PROVIDER_CURSOR_API},
            ) from exc
        finally:
            if agent is not None:
                try:
                    agent.close()
                except Exception:
                    pass

        status = str(getattr(getattr(result, "status", ""), "value", getattr(result, "status", "")))
        if status.lower() not in {"finished", "success", "completed"}:
            raise ZeError(
                "model_call_failed",
                f"Cursor Agent 未成功完成: {status or 'unknown'}",
                context={"provider": PROVIDER_CURSOR_API, "run_id": str(getattr(result, "id", ""))},
            )
        raw = str(getattr(result, "result", "") or "")
        payload = _scrape_structured_json(raw)
        if payload is None:
            raise ZeError(
                "model_call_failed",
                f"Cursor output is not JSON: {safe_error(raw)[:200]}",
                context={"provider": PROVIDER_CURSOR_API},
            )
        return payload


def user_llm_schema_call(
    *,
    provider: str,
    prompt: str,
    output_schema: dict[str, Any],
    config: Any | None = None,
    image_paths: list[str | Path] | tuple[()] = (),
    video_paths: list[str | Path] | tuple[()] = (),
    model: str = "",
    timeout: int = 180,
    cwd: str | Path | None = None,
    actor: Any | None = None,
    credential_store: CredentialStore | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Structured LLM call billed to the acting user's selected account."""
    _require_model_scope(MODEL_SCOPE_LLM, actor=actor)
    aliases = {
        "codex": PROVIDER_CODEX_CLI,
        "claude": PROVIDER_CLAUDE_CLI,
        "cursor": PROVIDER_CURSOR_API,
    }
    selected = aliases.get(str(provider or "").strip().lower(), str(provider or "").strip())
    if selected not in {PROVIDER_CODEX_CLI, PROVIDER_CLAUDE_CLI, PROVIDER_CURSOR_API}:
        raise ZeError(
            "validation_failed",
            "未知提示词优化账号",
            context={"provider": selected, "allowed": sorted(aliases.values())},
        )
    uid, store, rec = _bound_user_credential(
        selected, actor=actor, credential_store=credential_store
    )
    images = [Path(path).expanduser().resolve() for path in image_paths]
    missing = [str(path) for path in images if not path.is_file()]
    if missing:
        raise ZeError("validation_failed", "图片不存在", context={"missing": missing[:5]})
    videos = [Path(path).expanduser().resolve() for path in video_paths]
    missing_videos = [str(path) for path in videos if not path.is_file()]
    if missing_videos:
        raise ZeError("validation_failed", "视频不存在",
                      context={"missing": missing_videos[:5]})
    if videos and selected not in {
        PROVIDER_CODEX_CLI, PROVIDER_CLAUDE_CLI, PROVIDER_CURSOR_API,
    }:
        raise ZeError(
            "validation_failed",
            "当前只有 Codex / Claude / Cursor 提示词优化支持自行读取视频",
            context={"provider": selected},
        )
    try:
        if selected == PROVIDER_CURSOR_API:
            secret = store.reveal_secret(uid, rec.id)
            return _cursor_sdk_schema_call(
                prompt=prompt,
                output_schema=output_schema,
                api_key=secret,
                model=model,
                image_paths=images,
                video_paths=videos,
                timeout=timeout,
                cancel_event=cancel_event,
            )
        home = str(rec.metadata.get(META_HOME_DIR) or "")
        if not home:
            raise ZeError(
                "requires_credential",
                "AI 账号绑定缺少隔离目录,请重新绑定",
                context={"provider": selected},
            )
        cfg = config
        get = getattr(cfg, "get", None)
        if selected == PROVIDER_CODEX_CLI:
            binary = str(get("ze.llm.codex_bin", "codex") if callable(get) else "codex")
            return llm_schema_call(
                engine="codex", binary=binary, prompt=prompt, output_schema=output_schema,
                image_paths=images, video_paths=videos,
                model=model, timeout=timeout, cwd=cwd,
                env=cli_home_env(selected, home),
            )
        binary = str(get("ze.llm.claude_bin", "claude") if callable(get) else "claude")
        return llm_schema_call(
            engine="claude", binary=binary, prompt=prompt, output_schema=output_schema,
            image_paths=images, video_paths=videos,
            model=model, timeout=timeout, cwd=cwd,
            env=cli_home_env(selected, home),
        )
    finally:
        store.mark_used(uid, rec.id)


def cursor_models_for_current_user(
    *,
    actor: Any | None = None,
    credential_store: CredentialStore | None = None,
) -> list[dict[str, Any]]:
    """Return the account-specific Cursor model catalog without exposing its key."""
    uid, store, rec = _bound_user_credential(
        PROVIDER_CURSOR_API, actor=actor, credential_store=credential_store
    )
    secret = store.reveal_secret(uid, rec.id)
    try:
        from cursor_sdk import Cursor
    except ImportError as exc:
        raise ZeError(
            "dependency_unavailable",
            "Cursor SDK 未安装;请准备 python-runtime 依赖环境",
            context={"dependency": "cursor-sdk"},
        ) from exc
    try:
        rows = Cursor.models.list(api_key=secret)
    except Exception as exc:
        message = str(exc).replace(secret, "[REDACTED]") if secret else str(exc)
        raise ZeError(
            "model_call_failed",
            f"Cursor 模型目录读取失败: {message[:500]}",
            context={"provider": PROVIDER_CURSOR_API,
                     "retryable": bool(getattr(exc, "is_retryable", False))},
        ) from exc
    finally:
        store.mark_used(uid, rec.id)
    return [
        {
            "id": str(row.id),
            "display_name": str(row.display_name),
            "description": str(row.description or ""),
        }
        for row in rows
    ]


# ==============================================================
# agent_chat — 一轮 agentic 对话(浮层 AI 协作面板的引擎)
#
# 与 llm_call(单发文本补全)不同:这里让本机 CLI **agentic 操作当前工具**——把工具的
# MCP surface(loopback `/mcp`)作为工具集喂给 claude -p / codex exec,模型自己调命令改实体,
# 改动经 mcp_bridge dispatch(source="ai")→ OpLog → Broker → SSE 实时渲染。本函数只做**机制**:
# 拼 CLI 参数 / per-user env / 子进程 / 解析最终助手文本。system prompt 内容、开放哪些命令、
# 用哪个引擎全由 caller(server /api/ai/chat)决定。
#
# 铁律:
#   - **绝不持有 ctx.lock**(本函数不碰 repo/Context;子进程的 MCP 回调在 server 另一线程各自
#     短暂持锁,若 caller 在等子进程时持锁=跨线程死锁,见 model SPEC / 计划三硬约束②)。
#   - 子进程**只经 loopback /mcp 写**,FS 层 sandbox 只读(claude 只放行 mcp__ze 工具、codex
#     --sandbox read-only),绝不直接写 ZE_DATA_DIR(单写不变量)。
#   - per-user env(CLAUDE_CONFIG_DIR/CODEX_HOME)由 caller 传入;缺失时 caller 决定 fail-closed
#     还是本机兜底(server 端多租户 fail-closed,本地开发兜底)。
# ==============================================================

# 一次性 Bearer 走子进程 env 的变量名(claude + codex 共用同一名,各自独立子进程互不干扰)。
# 非 ZE_ 前缀:这是 CLI 传输实现细节、非 ZE 配置(避开 ops config_schema 门禁),与 codex 路线一致。
_MCP_BEARER_TOKEN_ENV = "MCP_BEARER_TOKEN"


def _claude_mcp_config(mcp_url: str, mcp_token: str) -> dict[str, Any]:
    server: dict[str, Any] = {"type": "http", "url": mcp_url}
    if mcp_token:
        # token 走子进程 env、不进 argv(ps 看不到、不落盘):header 值只放 ${VAR} 引用,
        # claude CLI 的 --mcp-config 对 http headers 值做环境变量展开(o36:${VAR}→process.env[VAR],
        # 缺失则保留字面 ${VAR} 触发 401 而非空头);真 token 由 _claude_agent_chat 注入该 env。
        server["headers"] = {"Authorization": "Bearer ${" + _MCP_BEARER_TOKEN_ENV + "}"}
    return {"mcpServers": {"ze": server}}


def agent_chat(*, engine: str, binary: str, prompt: str, mcp_url: str,
               mcp_token: str = "", system: str = "", model: str = "",
               env: dict[str, str] | None = None, timeout: int = 300,
               cwd: str | Path | None = None,
               image_paths: list[str | Path] | tuple = ()) -> dict[str, Any]:
    """Run ONE agentic turn where the CLI operates this tool via loopback MCP.

    Returns ``{"reply": <final assistant text>, "engine": engine}``. The tool's
    entity mutations happen as a side effect (the model calls the ze MCP tools);
    this function only returns the assistant's closing message for the chat bubble.

    ``image_paths`` = 用户附带的参考图(已由 caller 暂存成本地文件;claude 走 Read+--add-dir
    只读那个暂存目录,codex 走 -i)。
    """
    engine = str(engine or "").lower()
    if engine not in {"claude", "codex"}:
        raise ZeError("validation_failed", f"未知 agentic 引擎: {engine!r}",
                      context={"allowed": ["claude", "codex"]})
    if not binary or "\x00" in binary or "\n" in binary:
        raise ZeError("validation_failed", "非法 CLI 可执行配置", context={})
    if not mcp_url or "\x00" in mcp_url or "\n" in mcp_url:
        raise ZeError("validation_failed", "非法 MCP URL", context={})
    images = [Path(p).expanduser().resolve() for p in image_paths]
    missing = [str(p) for p in images if not p.is_file()]
    if missing:
        raise ZeError("validation_failed", "参考图不存在", context={"missing": missing[:5]})
    run_env = {**os.environ, **(env or {})}
    if engine == "claude":
        return _claude_agent_chat(binary, prompt, mcp_url, mcp_token, system, model,
                                  run_env, timeout, images)
    return _codex_agent_chat(binary, prompt, mcp_url, mcp_token, system, model,
                             run_env, timeout, cwd, images)


def _claude_agent_chat(binary: str, prompt: str, mcp_url: str, mcp_token: str,
                       system: str, model: str, env: dict[str, str],
                       timeout: int, images: list[Path]) -> dict[str, Any]:
    # 只放行 ze MCP 工具(mcp__ze)——模型能调本工具全部 AI 命令,但拿不到 Bash/Edit/Write。
    # --strict-mcp-config 只加载我们注入的 MCP、忽略宿主 MCP;--disable-slash-commands 关掉宿主
    # skills(缺 frontmatter 的 SKILL.md 会崩 session)。有参考图时额外放行 Read + --add-dir 暂存目录
    # (**只读那个目录**,不扩别处 FS;所有写仍只经 /mcp)。
    allowed = "mcp__ze"
    args = [
        binary, "-p",
        "--output-format", "json",
        "--mcp-config", json.dumps(_claude_mcp_config(mcp_url, mcp_token), ensure_ascii=False),
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    if images:
        allowed = "mcp__ze Read"
        for directory in sorted({str(p.parent) for p in images}):
            args += ["--add-dir", directory]
        prompt = (prompt + "\n\n参考图片(用 Read 工具查看):\n"
                  + "\n".join(str(p) for p in images))
    args += ["--allowedTools", allowed]
    if model:
        args += ["--model", model]
    if system:
        args += ["--append-system-prompt", system]
    child_env = dict(env)
    if mcp_token:
        # 真 token 只进子进程 env(_claude_mcp_config 的 header 只留 ${VAR} 引用),不进 argv。
        child_env[_MCP_BEARER_TOKEN_ENV] = mcp_token
    envelope = _run_agent_cli(binary, args, prompt, child_env, timeout, redact=mcp_token)
    if envelope.get("is_error"):
        raise ZeError("model_call_failed", f"claude error: {str(envelope.get('result'))[:400]}",
                      context={"binary": binary})
    return {"reply": str(envelope.get("result") or ""), "engine": "claude"}


def _toml_escape(v: str) -> str:
    """Escape a string for a quoted TOML `-c key="value"` override(挡 " / \\ 破引号)。"""
    return v.replace("\\", "\\\\").replace('"', '\\"')


def _redact(text: str, *secrets: str) -> str:
    """把已知机密(临时 token 等)从错误尾部抹掉,再进 ZeError → HTTP 响应/日志。"""
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


def _codex_agent_chat(binary: str, prompt: str, mcp_url: str, mcp_token: str,
                      system: str, model: str, env: dict[str, str],
                      timeout: int, cwd: str | Path | None,
                      images: list[Path]) -> dict[str, Any]:
    # codex 0.125 实测:HTTP MCP(url→streamable_http)的鉴权**不支持字面 bearer_token**
    # (报 "bearer_token is not supported for streamable_http"),须用 bearer_token_env_var 指一个
    # env 变量,codex 读它发 Authorization: Bearer(与 claude 路线的 headers.Authorization 等价)。
    work = str(Path(cwd).expanduser().resolve()) if cwd is not None else str(Path.cwd())
    # -c 值转义:今天 mcp_url(urlencoded)/ mcp_token(hex)都不含引号,仍显式转义,
    # 防将来输入源变化时 " / \\ 破 TOML 引号成 config 注入点(见 code-review LOW)。
    # codex 的 --output-last-message 把 agent 最终回复**直接写文件**——不依赖 --json 事件格式
    # (codex 0.125 事件是 agentMessage 驼峰/可嵌套,旧的 stdout JSONL 解析会漏 → 空气泡)。
    out_fd, last_msg_path = tempfile.mkstemp(prefix="ze-codex-out-", suffix=".txt")
    os.close(out_fd)
    args = [
        binary, "exec", "--skip-git-repo-check", "--json",
        "--sandbox", "read-only", "--color", "never",
        "-o", last_msg_path,
        "-c", "skills.enabled=false",
        "-c", f'mcp_servers.ze.url="{_toml_escape(mcp_url)}"',
    ]
    run_env = dict(env)
    if mcp_token:
        # token 走 env(不进 argv/配置文件,ps 看不到、不落盘),codex 从该 env 变量读出发 Bearer 头。
        # 共用 _MCP_BEARER_TOKEN_ENV(与 claude 路线同名,子进程各自独立)。
        run_env[_MCP_BEARER_TOKEN_ENV] = mcp_token
        args += ["-c", f'mcp_servers.ze.bearer_token_env_var="{_MCP_BEARER_TOKEN_ENV}"']
    if model:
        args += ["-m", model]
    for image in images:
        args += ["-i", str(image)]
    full = f"{system}\n\n{prompt}" if system else prompt
    args += ["-C", work, full]
    try:
        try:
            # per-home 互斥:与生图路线共享同一 CODEX_HOME 时串行,防 OAuth refresh 互踢(3.56)。
            with _user_cli_lock(run_env.get("CODEX_HOME", "")):
                proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=run_env)
        except FileNotFoundError as e:
            raise ZeError("model_call_failed", f"找不到 CLI 可执行: {binary!r}",
                          context={"binary": binary}) from e
        except subprocess.TimeoutExpired as e:
            raise ZeError("model_call_failed", f"codex exec 超时({timeout}s)",
                          context={"binary": binary}) from e
        if proc.returncode != 0:
            tail = "\n".join(s for s in ((proc.stderr or "").strip(), (proc.stdout or "").strip()) if s)
            raise ZeError("model_call_failed",
                          f"codex exec 退出码 {proc.returncode}: {_redact(tail[-1200:], mcp_token)}",
                          context={"binary": binary, "returncode": proc.returncode})
        # 优先读 codex 官方最终消息文件;为空再回退解析 stdout JSONL(旧机制,双保险)。
        reply = ""
        try:
            reply = Path(last_msg_path).read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if not reply:
            reply = _codex_last_message(proc.stdout or "")
        return {"reply": reply, "engine": "codex"}
    finally:
        try:
            os.unlink(last_msg_path)
        except OSError:
            pass


def _run_agent_cli(binary: str, args: list[str], prompt: str,
                   env: dict[str, str], timeout: int, redact: str = "") -> dict[str, Any]:
    """Run the CLI (prompt via stdin), parse the `--output-format json` envelope.

    ``redact`` = 临时 token,从错误尾部抹掉(CLI 若把鉴权头打进 stderr 才有风险,防泄)。"""
    try:
        proc = subprocess.run(args, input=prompt, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except FileNotFoundError as e:
        raise ZeError("model_call_failed", f"找不到 CLI 可执行: {binary!r}",
                      context={"binary": binary}) from e
    except subprocess.TimeoutExpired as e:
        raise ZeError("model_call_failed", f"agentic CLI 超时({timeout}s): {binary!r}",
                      context={"binary": binary}) from e
    if proc.returncode != 0:
        tail = "\n".join(s for s in ((proc.stderr or "").strip(), (proc.stdout or "").strip()) if s)
        raise ZeError("model_call_failed",
                      f"CLI 退出码 {proc.returncode}: {_redact(tail[-1200:], redact)}",
                      context={"binary": binary, "returncode": proc.returncode})
    envelope = _scrape_structured_json(proc.stdout or "")
    if envelope is None:
        raise ZeError("model_call_failed", f"CLI output is not JSON: {(proc.stdout or '')[:200]}",
                      context={"binary": binary})
    return envelope


def _codex_last_message(raw: str) -> str:
    """codex exec --json → 末条 agent/assistant 文本(容忍逐行 JSONL + 噪声)。"""
    last = ""
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        # codex 事件形态随版本变;抓最后一个带文本的 agent/assistant/message 事件。
        for key in ("last_agent_message", "message", "text", "content"):
            val = evt.get(key) if isinstance(evt, dict) else None
            if isinstance(val, str) and val.strip():
                last = val.strip()
    return last


__all__ = [
    "llm_call", "image_call", "llm_schema_call", "user_llm_schema_call",
    "cursor_models_for_current_user", "agent_chat",
]
