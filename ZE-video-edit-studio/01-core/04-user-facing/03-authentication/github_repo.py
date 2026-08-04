"""Read-only GitHub repository client for the per-user ``github_repo`` credential.

系统 1b「github 仓库关联」的服务端只读后端:用绑定用户自己的 PAT 调 GitHub Contents
API 列目录 / 读文件。红线:

  - **只读**:只 GET /repos/{owner}/{repo}/contents/{path}?ref= 与 git/blobs;从不写 GitHub。
  - **token 只在本模块内当 Authorization 头用**,绝不回传前端、绝不写进 ZeError context。
  - **rate-limit 感知**:读 X-RateLimit-* → 超限抛结构化 ``github_rate_limited``,不静默。
  - **诚实报错**:token 失效 → ``github_unauthorized``;私库无权 → ``github_forbidden``;
    路径/仓库不存在 → ``github_not_found``;网络失败 → ``github_unavailable``。
  - **超大 / 二进制**:超上限或无法 UTF-8 解码 → 结构化拒(``github_file_too_large`` /
    ``github_binary_file``),不把二进制塞进文本通道。

``_http_get`` 是唯一网络接缝,单测 monkeypatch 它注入 mock 响应,不发真网络。
"""

from __future__ import annotations

import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# 共享 ZeError:从 02-entity 兄弟目录 import(v3 平铺布局,同 auth.py 的接法)。
_ENTITY_DIR = Path(__file__).resolve().parent.parent.parent / "01-data-structure" / "02-entity"
if str(_ENTITY_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTITY_DIR))

from entity import ZeError  # noqa: E402

GITHUB_API_ROOT = "https://api.github.com"
_DEFAULT_TIMEOUT = 15.0
# 文本读取上限:超过即结构化拒(不把超大 blob 塞进 MCP 通道)。
DEFAULT_MAX_FILE_BYTES = 512 * 1024
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")


def validate_owner(owner: Any) -> str:
    o = str(owner or "").strip()
    if not _OWNER_RE.match(o):
        raise ZeError("validation_failed", "GitHub owner 名不合法", context={"field": "owner"})
    return o


def validate_repo(repo: Any) -> str:
    r = str(repo or "").strip()
    if r in (".", "..") or not _REPO_RE.match(r):
        raise ZeError("validation_failed", "GitHub repo 名不合法", context={"field": "repo"})
    return r


def validate_ref(ref: Any) -> str:
    v = str(ref or "").strip()
    if not v:
        return ""
    if ".." in v or v.startswith("/") or v.endswith("/") or not _REF_RE.match(v):
        raise ZeError("validation_failed", "GitHub ref 不合法", context={"field": "ref"})
    return v


def normalize_path(path: Any) -> str:
    """把相对仓库路径规范化;拒绝穿越(``..``)、绝对路径与超长片段。"""
    p = str(path or "").strip().replace("\\", "/").lstrip("/")
    if len(p) > 1024:
        raise ZeError("validation_failed", "GitHub 路径过长", context={"field": "path"})
    segments = [seg for seg in p.split("/") if seg != ""]
    for seg in segments:
        if seg in (".", "..") or len(seg) > 255:
            raise ZeError("validation_failed", "GitHub 路径含非法片段", context={"field": "path"})
    return "/".join(segments)


def _http_get(url: str, token: str, timeout: float = _DEFAULT_TIMEOUT) -> tuple[int, dict, bytes]:
    """唯一网络接缝(测试 monkeypatch 本函数)。token 只作 Authorization 头,不入返回值。"""
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {token}",
        "Accept": _ACCEPT,
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": "ZE-skill-build",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", None) or resp.getcode() or 0)
            return status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001 - 错误体读不到也要给出状态码
            body = b""
        return int(exc.code), dict((exc.headers or {}).items()), body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ZeError(
            "github_unavailable", "GitHub 暂时无法访问",
            context={"reason": exc.__class__.__name__},
        ) from exc


def _header(headers: dict, name: str) -> str:
    lname = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lname:
            return str(value)
    return ""


def _rate_meta(headers: dict) -> dict[str, str]:
    return {
        "remaining": _header(headers, "X-RateLimit-Remaining"),
        "reset": _header(headers, "X-RateLimit-Reset"),
    }


def _raise_for_status(status: int, headers: dict) -> None:
    if status == 200:
        return
    if status in (403, 429) and (
        _header(headers, "X-RateLimit-Remaining") == "0" or _header(headers, "Retry-After")
    ):
        raise ZeError(
            "github_rate_limited", "GitHub API 速率已达上限,请稍后再试",
            context={"reset": _header(headers, "X-RateLimit-Reset")},
        )
    if status == 401:
        raise ZeError("github_unauthorized", "GitHub 令牌无效或已过期", context={})
    if status == 403:
        raise ZeError("github_forbidden", "无权访问该 GitHub 仓库(私库需令牌具备 repo 权限)", context={})
    if status == 404:
        raise ZeError("github_not_found", "GitHub 仓库或路径不存在", context={})
    raise ZeError("github_error", "GitHub API 返回异常", context={"status": status})


def _contents_url(owner: str, repo: str, path: str, ref: str) -> str:
    url = (
        f"{GITHUB_API_ROOT}/repos/{urllib.parse.quote(owner)}"
        f"/{urllib.parse.quote(repo)}/contents/{urllib.parse.quote(path)}"
    )
    if ref:
        url += "?" + urllib.parse.urlencode({"ref": ref})
    return url


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ZeError("github_error", "GitHub 响应解析失败", context={}) from exc


def _dir_payload(owner: str, repo: str, ref: str, path: str,
                 items: list, headers: dict) -> dict[str, Any]:
    entries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entries.append({
            "name": str(item.get("name", "")),
            "path": str(item.get("path", "")),
            "type": str(item.get("type", "")),
            "size": int(item.get("size") or 0),
        })
    return {
        "kind": "dir", "owner": owner, "repo": repo, "ref": ref, "path": path,
        "entries": entries, "rate_limit": _rate_meta(headers),
    }


def _read_blob(token: str, owner: str, repo: str, sha: str, max_bytes: int) -> bytes:
    if not sha:
        raise ZeError("github_error", "GitHub 大文件缺少 blob sha", context={})
    url = (
        f"{GITHUB_API_ROOT}/repos/{urllib.parse.quote(owner)}"
        f"/{urllib.parse.quote(repo)}/git/blobs/{urllib.parse.quote(sha)}"
    )
    status, headers, body = _http_get(url, token)
    _raise_for_status(status, headers)
    obj = _parse_json(body)
    if not isinstance(obj, dict):
        raise ZeError("github_error", "GitHub blob 响应异常", context={})
    if int(obj.get("size") or 0) > max_bytes:
        raise ZeError(
            "github_file_too_large", "GitHub blob 超过读取上限",
            context={"size": int(obj.get("size") or 0), "max_bytes": max_bytes},
        )
    content = obj.get("content")
    if str(obj.get("encoding")) != "base64" or not isinstance(content, str):
        raise ZeError("github_error", "GitHub blob 编码异常", context={})
    return base64.b64decode(content)


def _file_payload(token: str, owner: str, repo: str, ref: str, obj: dict,
                  headers: dict, max_bytes: int) -> dict[str, Any]:
    size = int(obj.get("size") or 0)
    sha = str(obj.get("sha") or "")
    path = str(obj.get("path") or "")
    if size > max_bytes:
        raise ZeError(
            "github_file_too_large", "文件超过读取上限",
            context={"size": size, "max_bytes": max_bytes, "sha": sha, "path": path},
        )
    encoding = str(obj.get("encoding") or "")
    content = obj.get("content")
    if encoding == "base64" and isinstance(content, str) and content:
        raw = base64.b64decode(content)
    else:
        # 超大文件 contents API 返回 encoding='none' → 转 blob API 取。
        raw = _read_blob(token, owner, repo, sha, max_bytes)
    if len(raw) > max_bytes:
        raise ZeError(
            "github_file_too_large", "文件超过读取上限",
            context={"size": len(raw), "max_bytes": max_bytes, "sha": sha, "path": path},
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ZeError(
            "github_binary_file", "二进制文件无法作为文本读取",
            context={"size": size, "sha": sha, "path": path},
        ) from exc
    return {
        "kind": "file", "owner": owner, "repo": repo, "ref": ref, "path": path,
        "size": size, "sha": sha, "text": text, "rate_limit": _rate_meta(headers),
    }


def read_path(token: str, owner: str, repo: str, path: str, ref: str = "",
              *, max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> dict[str, Any]:
    """列目录(``kind='dir'``)或读文件(``kind='file'``)。只读、结构化报错。"""
    owner = validate_owner(owner)
    repo = validate_repo(repo)
    ref = validate_ref(ref)
    path = normalize_path(path)
    status, headers, body = _http_get(_contents_url(owner, repo, path, ref), token)
    _raise_for_status(status, headers)
    parsed = _parse_json(body)
    if isinstance(parsed, list):
        return _dir_payload(owner, repo, ref, path, parsed, headers)
    if isinstance(parsed, dict) and parsed.get("type") == "file":
        return _file_payload(token, owner, repo, ref, parsed, headers, max_bytes)
    kind = parsed.get("type") if isinstance(parsed, dict) else "unknown"
    raise ZeError(
        "github_unsupported_type", "不支持的 GitHub 内容类型",
        context={"type": str(kind)},
    )


__all__ = [
    "read_path", "normalize_path",
    "validate_owner", "validate_repo", "validate_ref",
    "DEFAULT_MAX_FILE_BYTES", "GITHUB_API_ROOT",
]
