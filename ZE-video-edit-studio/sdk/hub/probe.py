"""Provider 健康探活(ProviderHub 回收 P3:登记后主动 HTTP 校验连通/鉴权)。

原型 provider.test 的**忠实**回收——但不复制其 SSRF-盲发:原型对用户自填 baseUrl 零校验直 GET;
这里探活的目标 URL 必须由 caller(server 端点)**先经运行时 SSRF 白名单校验**(server._credential_base_url:
https + host∈allowlist + port∈{None,443} + 无 user/pass/query/fragment)再拼 descriptor.endpoint_policy
['test_path'] 传入。本模块**不自己决定打哪个 host**——SSRF 边界留在 caller,这里只做 GET + 健康分类。

探活是主动 I/O,归 server 层(不进纯函数 resolver);只碰连接健康、不写 canonical 模型表(教义①/②/③)。
分类 → hub 9 类统一错误码,与 resolver/回执同源(前后端一次成型)。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from . import errors

# descriptor kind → 最小校验请求的鉴权方式(镜像 model.py 各 provider 调用约定;probe 只发 GET /models 级最小校验)。
# 官方 CLI(official_login)与内网服务(internal-service)无用户端点 → build_probe_request 返 None(caller 记 not_applicable)。
_PROBE_AUTH_KINDS = frozenset({"openai-compatible", "anthropic", "gemini"})


def is_probeable(kind: str) -> bool:
    """该 descriptor kind 是否有可探活的用户端点(官方 CLI / 内网服务 → False)。
    caller 用它在 reveal_secret **之前**判定,不可探活就不解密(最小暴露)。"""
    return kind in _PROBE_AUTH_KINDS


def build_probe_request(kind: str, base_url: str, secret: str, *,
                        test_path: str = "/v1/models") -> "Optional[tuple[str, dict[str, str]]]":
    """(descriptor kind, 已校验 base_url, secret) → (url, headers) 或 None(不可探活的 kind)。纯函数,可测。

    base_url 必须由 caller 先经运行时 SSRF 白名单校验(server._credential_base_url);本函数只按 kind 拼
    最小校验 URL + 鉴权头。openai-compatible=Bearer;anthropic=x-api-key+版本;gemini=key 入 query(同
    gemini_validate_key 约定)。secret 只进 header/query 传给已白名单的 host,不落盘、不回显。"""
    base = (base_url or "").rstrip("/")
    if kind == "openai-compatible":
        return base + test_path, {"Authorization": f"Bearer {secret}"}
    if kind == "anthropic":
        return base + (test_path or "/v1/models"), {"x-api-key": secret, "anthropic-version": "2023-06-01"}
    if kind == "gemini":
        return (base + "/v1beta/models?pageSize=1&key=" + urllib.parse.quote(secret or "", safe=""), {})
    return None


@dataclass(frozen=True)
class ProbeResult:
    """一次探活结果(**不含 secret**;health 供 UI 显示,error_code 与 hub 错误码同源)。"""
    ok: bool
    health: str                              # "ok" | "error"
    error_code: Optional[str] = None         # status="error" 时的 9 类码之一
    detail: str = ""                         # 人读简述(HTTP NNN / 网络失败),不含 secret
    latency_ms: Optional[int] = None


def classify_http_status(status: int) -> tuple[bool, Optional[str]]:
    """HTTP 状态码 → (ok, hub 错误码)。纯函数,可离线穷举测。

    2xx→ok;401/403→credential_expired(key 无效/未授权);429→rate_limited;5xx→provider_unreachable
    (厂商端故障);其它 4xx→request_invalid(请求本身问题,如 404 路径错)。"""
    if 200 <= status < 300:
        return True, None
    if status in (401, 403):
        return False, errors.CREDENTIAL_EXPIRED
    if status == 429:
        return False, errors.RATE_LIMITED
    if 500 <= status < 600:
        return False, errors.PROVIDER_UNREACHABLE
    return False, errors.REQUEST_INVALID


def _guarded_open(req, *, timeout):
    """出站统一入口:跟随重定向,但每一跳都过 SSRF 护栏。

    刻意不直接调 urllib.request.urlopen —— 它默认静默跟随 302,登记期/发请求前的两道护栏
    对重定向后的地址一次都问不到(实测:合规 host 回 Location: http://169.254.169.254/… 会被跟进)。
    """
    from .endpoint_guard import guarded_opener
    return guarded_opener().open(req, timeout=timeout)


def _result(ok: bool, code: Optional[str], detail: str, latency_ms: Optional[int]) -> ProbeResult:
    return ProbeResult(ok=ok, health="ok" if ok else "error",
                       error_code=code, detail=detail, latency_ms=latency_ms)


def probe_url(url: str, *, headers: Optional[dict[str, str]] = None, timeout: int = 15) -> ProbeResult:
    """对**已由 caller 经 SSRF 白名单校验**的 url 发 GET,返回健康分类。真 I/O(测试须 mock urlopen)。

    caller 保证:url 的 host 在 provider allowlist 内(经 _credential_base_url);headers 里的鉴权由
    caller 用 reveal_secret 注入(本模块不接触 CredentialStore)。网络失败(URLError/超时)→ provider_unreachable。"""
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    started = time.perf_counter()
    try:
        with _guarded_open(req, timeout=timeout) as resp:
            resp.read()
            elapsed = int((time.perf_counter() - started) * 1000)
            ok, code = classify_http_status(getattr(resp, "status", 200) or 200)
            return _result(ok, code, f"HTTP {getattr(resp, 'status', 200)}", elapsed)
    except urllib.error.HTTPError as e:
        elapsed = int((time.perf_counter() - started) * 1000)
        ok, code = classify_http_status(e.code)
        return _result(ok, code, f"HTTP {e.code}", elapsed)
    except (urllib.error.URLError, TimeoutError) as e:
        elapsed = int((time.perf_counter() - started) * 1000)
        return _result(False, errors.PROVIDER_UNREACHABLE, f"网络失败: {e}", elapsed)


def parse_discovered_models(payload: Any, kind: str) -> list[str]:
    """provider /models 响应 → model_id 列表(纯函数,per-kind 形状)。坏结构/缺键 → []。

    openai-compatible/anthropic:{"data":[{"id":...}]};gemini:{"models":[{"name":"models/xxx"}]}
    (剥 'models/' 前缀)。去重保序;非 str/空 id 跳过。"""
    if not isinstance(payload, dict):
        return []
    if kind == "gemini":
        rows = payload.get("models", [])
        raw = [str(r.get("name", "")).split("/")[-1] for r in rows if isinstance(r, dict)]
    else:  # openai-compatible / anthropic
        rows = payload.get("data", [])
        raw = [str(r.get("id", "")) for r in rows if isinstance(r, dict)]
    seen: set[str] = set()
    return [m for m in raw if m and not (m in seen or seen.add(m))]


def discover_models(url: str, *, headers: Optional[dict[str, str]] = None, kind: str,
                    timeout: int = 15) -> "tuple[list[str], Optional[str]]":
    """对**已经 caller SSRF 白名单校验**的 url GET /models 并解析 → (model_ids, error_code)。

    非抛错风格(同 probe_url):成功 (ids, None);HTTP 错→([], 分类码);网络失败→([], provider_unreachable);
    JSON 解析失败→([], request_invalid)。SSRF 边界与 secret 注入在 caller,本函数不决定 host。"""
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        with _guarded_open(req, timeout=timeout) as resp:
            ok, code = classify_http_status(getattr(resp, "status", 200) or 200)
            if not ok:
                return [], code
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return [], classify_http_status(e.code)[1]
    except (urllib.error.URLError, TimeoutError):
        return [], errors.PROVIDER_UNREACHABLE
    except (ValueError, json.JSONDecodeError):
        return [], errors.REQUEST_INVALID
    return parse_discovered_models(payload, kind), None
