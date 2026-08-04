"""sdk/lark_client.py — 飞书能力薄驱动（统一收口：发消息 / 读文档）。

把散落各处手写的 lark-cli 机制收成一组薄函数：泥采(lark-bot)的收发、batch-image-foundry
的 feishu_doc、sdk/notify.py 的飞书后端，以及 .54 哨兵的告警出口，共用这一层。机制归这里，
文案/收件人/路由等策略留各调用方。

设计同 notify.py 红线：
- 只做机制（定位 lark-cli / 发 / 读 / 解析），不 import 飞书 SDK、只用 stdlib subprocess。
- **密钥永不进 argv / 日志**：app_secret 由 lark-cli 自身 config 持（部署期 `lark-cli config init`），
  本模块只调 CLI、绝不读写密钥。list 形式 subprocess（无 shell，元字符天然失效）。
- 收件人 / 文档 token 走白名单字符，挡注入。

**可独立 import**：不硬依赖框架其它模块（ZeError 优雅兜底），故 .54 哨兵可直接拷本文件复用。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:  # 在 build-v3 内用框架 ZeError；独立(如 .54 哨兵)时退化成普通异常。
    from entity import ZeError  # type: ignore
except Exception:  # noqa: BLE001
    class ZeError(Exception):  # type: ignore
        def __init__(self, code: str, message: str = "", context: dict | None = None):
            super().__init__(f"[{code}] {message}")
            self.code, self.message, self.context = code, message, context or {}

_ID_RE = re.compile(r"[A-Za-z0-9_@.\-]+")          # chat_id / user_id 白名单
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")            # 文档 token 白名单
_MESSAGE_ID_RE = re.compile(r"om_[A-Za-z0-9]+")
# amber service PATH 漏 ~/.npm-global/bin → which 兜底（见 reference_amber_service_path_omits_npm_global）
_LARK_CLI_FALLBACKS = (
    "~/.npm-global/bin/lark-cli", "/usr/local/bin/lark-cli",
    "/opt/homebrew/bin/lark-cli", "/usr/bin/lark-cli",
)
_DEFAULT_MAX_DOC_CHARS = 40000


def resolve_lark_cli(config: Any = None) -> str:
    """定位 lark-cli：显式配置 `ze.notify.lark_cli_bin` > PATH > 常见目录兜底。"""
    explicit = ""
    if config is not None:
        try:
            explicit = str(config.get("ze.notify.lark_cli_bin", "") or "")
        except Exception:  # noqa: BLE001
            explicit = ""
    cand = str(Path(explicit).expanduser()) if explicit else (shutil.which("lark-cli") or "")
    if not cand:
        for c in _LARK_CLI_FALLBACKS:
            p = Path(c).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                cand = str(p)
                break
    if not cand:
        raise ZeError("lark_cli_not_found",
                      "找不到 lark-cli（配 ze.notify.lark_cli_bin 或装到 PATH）")
    if "\x00" in cand or "\n" in cand:
        raise ZeError("validation_failed", "非法 lark-cli 路径")
    return cand


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise ZeError("lark_cli_not_found", f"找不到 lark-cli: {args[0]!r}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"lark-cli 超时（{timeout}s）") from e


def _parse_message_id(stdout: str) -> str:
    text = (stdout or "").strip()
    if not text:
        return ""
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            mid = d.get("message_id") or (d.get("data") or {}).get("message_id")
            if mid:
                return str(mid)
    except json.JSONDecodeError:
        pass
    m = _MESSAGE_ID_RE.search(text)
    return m.group(0) if m else ""


def send(*, text: str | None = None, markdown: str | None = None,
         chat_id: str | None = None, user_id: str | None = None,
         config: Any = None, timeout: int = 30, idempotency_key: str | None = None) -> str:
    """以 bot 身份发一条消息到群(chat_id)或私聊(user_id)，返回 message_id。

    text / markdown 二选一（markdown 走 post 格式，可粗体/链接）。文案由调用方预渲染。
    """
    recipient, flag = (chat_id, "--chat-id") if chat_id else (user_id, "--user-id")
    if not recipient or not _ID_RE.fullmatch(str(recipient)):
        raise ZeError("validation_failed", "缺少或非法的 chat_id/user_id",
                      context={"recipient": recipient})
    body = markdown if markdown is not None else text
    if not body or not str(body).strip():
        raise ZeError("validation_failed", "消息正文为空")
    content_flag = "--markdown" if markdown is not None else "--text"
    binary = resolve_lark_cli(config)
    args = [binary, "im", "+messages-send", flag, str(recipient), content_flag, str(body), "--as", "bot"]
    if idempotency_key:
        args += ["--idempotency-key", str(idempotency_key)]
    proc = _run(args, timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"lark-cli 发送退出码 {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:300]}")
    return _parse_message_id(proc.stdout)


def fetch_doc(token_or_url: str, *, config: Any = None, timeout: int = 60,
              max_chars: int = _DEFAULT_MAX_DOC_CHARS) -> str:
    """以 bot 身份读飞书文档正文，返回纯文本/markdown（超长截断留可见标记）。

    兼容 lark-cli 版本差异：1.0.0 返回 data.markdown；较新版返回 data.document.content。
    """
    token = str(token_or_url or "").strip()
    # 允许直接传 URL：抽末段 token
    if "/" in token:
        token = token.rstrip("/").split("/")[-1].split("?")[0]
    if not token or not _TOKEN_RE.fullmatch(token):
        raise ZeError("validation_failed", "非法文档 token", context={"token": token_or_url})
    binary = resolve_lark_cli(config)
    proc = _run([binary, "docs", "+fetch", "--doc", token, "--as", "bot", "--format", "json"], timeout)
    try:
        data = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        raise ZeError("feishu_doc_failed",
                      f"飞书文档读取输出非 JSON: {(proc.stdout or proc.stderr or '')[:200]}")
    if data.get("ok") is False or proc.returncode != 0:
        msg = str((data.get("error") or {}).get("message") or proc.stderr or "读取失败")
        raise ZeError("feishu_doc_failed", f"飞书文档读取失败: {msg[:200]}")
    d = data.get("data") or {}
    body = str(d.get("markdown") or data.get("markdown")
               or (d.get("document") or {}).get("content") or "")
    if not body.strip():
        raise ZeError("feishu_doc_failed", "文档正文为空（可能未分享给该 bot）")
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n…[飞书文档过长，已截断，仅取前 {max_chars} 字]"
    return body
