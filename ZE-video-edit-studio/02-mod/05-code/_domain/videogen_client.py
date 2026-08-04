"""Video-generation service client — thin, zero-auth, raises on transport error.

Models the proven ZE-video-generation-timeline ``seedance_client`` contract but
generalised to the unified video-gen service (11 models). Discipline carried
over verbatim:
- The client RAISES raw transport exceptions (URLError/Timeout/OSError). It does
  NOT classify ``requires_runtime`` vs ``failed`` — that decision lives in the
  hosting skill's stage layer (client-raises / stage-classifies).
- One HTTP seam (``_urlopen``) so tests monkeypatch it and never touch network.
- Pure body/parse builders are unit-tested without a network.
- ``download`` rejects non-http(s) schemes and caps size.

Endpoints (env-overridable): submit ``POST {base}/v1/videos/generations``, poll
``GET {base}/v1/videos/{task_id}``(注意查询路径不带 generations)。地址只从 env 取,未配置即抛
ConnectionError —— 不在代码里写死内网地址。
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid
from typing import Any, Mapping

from . import capability_matrix

_urlopen = urllib.request.urlopen  # single seam; tests monkeypatch this

# No hardcoded internal endpoint — the service URL is deployment config, provided
# via env (or a 04-ops config knob upstream). Unconfigured → the client raises a
# ConnectionError, which the hosting stage classifies as requires_runtime.
BASE_URL_ENV_VARS = ("ZE_VIDEOGEN_BASE_URL", "VIDEOGEN_API_BASE_URL")
DOWNLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB cap
UPLOAD_MAX_BYTES = 20 * 1024 * 1024
SEEDANCE_MIN_DURATION = 4
SEEDANCE_MAX_DURATION = 15


class SubmissionValidationError(ValueError):
    """请求尚未发出就能确定的本地提交错误。"""


def base_url() -> str:
    for var in BASE_URL_ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value.rstrip("/")
    raise ConnectionError(
        "视频生成服务地址未配置;设置 env " + " 或 ".join(BASE_URL_ENV_VARS))


def clamp_duration(value: Any, *, lo: int = SEEDANCE_MIN_DURATION,
                   hi: int = SEEDANCE_MAX_DURATION) -> int:
    """Clamp a requested duration into the provider's accepted range.

    (Moved here from clip_math per the porting report — a provider contract, not
    generic timeline math.) Bad input → 10.
    """
    try:
        return max(lo, min(int(round(float(value))), hi))
    except (TypeError, ValueError):
        return 10


# --- pure builders -----------------------------------------------------------

def clamp_duration_for(model: str, value: Any) -> int:
    """把时长夹到**该模型真实支持**的范围;只收离散档位的模型(veo 4/6/8、grok 6/8)取最近一档。

    范围来自真值表,不再用 ``"seedance" in model.lower()`` 这种子串猜 ——
    那个判断绕过了模型表,别名和未来任何名字里带 seedance 的模型都会被误伤。
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 10
    try:
        options = capability_matrix.duration_options(model)
        bounds = capability_matrix.duration_bounds(model)
    except (KeyError, ValueError):
        return int(round(seconds))
    if options:
        return int(min(options, key=lambda o: abs(o - seconds)))
    if bounds:
        return int(round(max(bounds[0], min(seconds, bounds[1]))))
    return int(round(seconds))


def build_submit_body(*, model: str, mode: str, prompt: str, ratio: str | None = None,
                      duration: int | None = None,
                      file_refs: list[str] | None = None,
                      extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """组装提交用的 JSON body(纯函数,被测试断言)。

    字段名对齐服务 ``/discover`` 的 ``endpoints.generations``:``model`` / ``prompt`` /
    ``mode`` 必填,``files`` 收 URL 或 data URI,``aspect_ratio`` / ``duration`` 可选。
    ``extra`` 放模型特有的可选参数(如 omni 的 ``previous_interaction_id``)。

    ``ratio`` / ``duration`` 默认都不带 —— 由调用方按模型契约显式传入。Omni edit
    不能带 ``aspect_ratio``；若这里默认 ``16:9``，``layer_edit_stage`` 用
    ``submit_files(**compile_submission_request(...))`` 时会把未声明的 ratio
    偷偷补回去。

    **不发文档里没有的字段** —— 从前会带一个 ``async: true``,那是旧 jimeng 网关
    (jimeng-video-api v0.1)的参数;统一服务(Video Service v1.0)提交即异步返回
    202 + task_id,参数表里没有它。
    """
    body: dict[str, Any] = {"model": model, "mode": mode, "prompt": prompt}
    if ratio:
        body["aspect_ratio"] = ratio
    if duration is not None:
        body["duration"] = clamp_duration_for(model, duration)
    if file_refs:
        body["files"] = list(file_refs)
    for key, value in (extra or {}).items():
        if value not in (None, ""):
            body[key] = value
    return body


def build_multipart_fields(*, model: str, mode: str, prompt: str,
                           ratio: str | None = None,
                           duration: int | None = None,
                           extra: Mapping[str, Any] | None = None) -> list[tuple[str, str]]:
    """multipart 的文本字段序(纯函数)。字段名同 JSON 模式,文件走 ``file[]``。"""
    fields: list[tuple[str, str]] = [("model", model), ("mode", mode),
                                     ("prompt", prompt)]
    if ratio:
        fields.append(("aspect_ratio", ratio))
    if duration is not None:
        fields.append(("duration", str(clamp_duration_for(model, duration))))
    for key, value in (extra or {}).items():
        if value not in (None, ""):
            fields.append((key, str(value)))
    return fields


# 扩展名 → MIME。服务靠**每个 part 的 Content-Type** 判断这是图还是视频;
# 一律写 application/octet-stream 会被判成
# "File at index 0 has unrecognizable type"(实测 400)。
_MIME_BY_EXT = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
}


def mime_for(path: str) -> str:
    return _MIME_BY_EXT.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def build_multipart(fields: list[tuple[str, str]], file_paths: list[str],
                    boundary: str) -> bytes:
    """multipart/form-data body:文本字段 + ``file[]`` 二进制(按传入顺序 = 记号顺序)。

    每个文件 part 必须带**真实 MIME**(见 ``_MIME_BY_EXT``)—— 服务靠它认类型。
    """
    out = bytearray()
    for name, value in fields:
        out.extend(f"--{boundary}\r\n".encode())
        out.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.extend(f"{value}\r\n".encode())
    for path in file_paths:
        filename = os.path.basename(path)
        with open(path, "rb") as fh:
            payload = fh.read()
        out.extend(f"--{boundary}\r\n".encode())
        out.extend(
            f'Content-Disposition: form-data; name="file[]"; filename="{filename}"\r\n'.encode())
        out.extend(f"Content-Type: {mime_for(path)}\r\n\r\n".encode())
        out.extend(payload)
        out.extend(b"\r\n")
    out.extend(f"--{boundary}--\r\n".encode())
    return bytes(out)


def validate_upload_size(file_paths: list[str], *,
                         maximum: int = UPLOAD_MAX_BYTES) -> None:
    """在组装请求前拦住单个文件超过网关 20MB 上限的上传。"""
    for path in file_paths:
        size = os.path.getsize(path)
        if size > maximum:
            raise SubmissionValidationError(
                f"文件「{os.path.basename(path)}」为 {size / (1024 * 1024):.1f}MB，"
                f"超过视频服务单文件 {maximum // (1024 * 1024)}MB 上传限制；"
                "请缩短修改区间或压缩素材后重试"
            )


def submit_files(*, model: str, mode: str, prompt: str, file_paths: list[str],
                 ratio: str | None = None, duration: int | None = None,
                 extra: Mapping[str, Any] | None = None,
                 timeout: int = 300) -> dict[str, Any]:
    """带本地文件提交(multipart)。文件顺序即契约 —— 必须与记号编号同源。

    二进制走 multipart 而不是塞进 JSON 的 data URI:一段视频 base64 之后
    动辄几十 MB,JSON 体积和超时都不划算。服务两种都收(``/discover`` 明写)。
    """
    paths = list(file_paths)
    validate_upload_size(paths)
    boundary = "----ze" + uuid.uuid4().hex
    fields = build_multipart_fields(model=model, mode=mode, prompt=prompt, ratio=ratio,
                                    duration=duration, extra=extra)
    body = build_multipart(fields, paths, boundary)
    req = urllib.request.Request(
        f"{base_url()}/v1/videos/generations", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    with _urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def task_id_of(response: dict) -> str | None:
    return response.get("id") or response.get("task_id") or (response.get("data") or {}).get("id")


def status_of(response: dict) -> str | None:
    return response.get("status") or (response.get("data") or {}).get("status")


def result_url_of(response: dict) -> str | None:
    """产物 URL。统一服务完成时给的是顶层 ``video_url``。

    实测真响应(2026-07-26)::

        {"id": …, "status": "completed", "model": "seedance-2.0-fast",
         "video_url": "http://…/xxx.mp4", "completed_at": …}

    其余几种形状是兼容旧网关的容错 —— 从前只认那几种,于是任务明明 completed
    却取不到 URL,直接崩在 download 里。凭空猜响应形状的代价就是这样。
    """
    direct = response.get("video_url") or response.get("url")
    if direct:
        return str(direct)
    data = response.get("data")
    if isinstance(data, list) and data:
        return data[0].get("url") or data[0].get("video_url")
    if isinstance(data, dict):
        return data.get("url") or data.get("video_url")
    return None


def error_message_of(response: dict) -> str:
    return str(response.get("error") or response.get("message")
               or (response.get("data") or {}).get("error") or "generation failed")


# --- network (raises on transport failure) -----------------------------------

def submit(*, model: str, mode: str, prompt: str, ratio: str | None = None,
           duration: int | None = None, file_refs: list[str] | None = None,
           extra: Mapping[str, Any] | None = None,
           timeout: int = 60) -> dict[str, Any]:
    body = build_submit_body(model=model, mode=mode, prompt=prompt, ratio=ratio,
                             duration=duration, file_refs=file_refs, extra=extra)
    req = urllib.request.Request(
        f"{base_url()}/v1/videos/generations",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with _urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def poll(task_id: str, *, timeout: int = 30) -> dict[str, Any]:
    """查询任务状态。路径是 ``/v1/videos/{id}`` —— **不是** 提交那个路径加 id。

    这条是打真服务才发现的:单测里 _urlopen 是假的,拼错的 URL 永远不会 404。
    服务 /discover 的 endpoints.task_status 写的就是 ``GET /v1/videos/:id``。
    """
    req = urllib.request.Request(
        f"{base_url()}/v1/videos/{task_id}", method="GET")
    with _urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(url: str, *, timeout: int = 300) -> bytes:
    scheme = (url.split(":", 1)[0] or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"refusing to download non-http(s) url scheme {scheme!r}")
    chunks = bytearray()
    with _urlopen(url, timeout=timeout) as resp:
        while True:
            chunk = resp.read(128 * 1024)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > DOWNLOAD_MAX_BYTES:
                raise ValueError("download exceeds size cap")
    return bytes(chunks)
