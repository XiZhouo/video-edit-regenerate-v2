"""sdk/notify.py — 外发通知薄驱动(深化点 #3:补 config 已声明的飞书凭证的发送执行层).

类比 `sdk/model.py`:`04-ops` 已声明 `ze.feishu_app_id/secret` 与 `ze.notify.*`,但只有
声明、没有驱动——每个要发飞书的 skill 都得自己 shell lark-cli。本模块把「按渠道分发 + 发送 +
retry + dry-run + NotifyLog 留痕」收成一组薄函数,server 路由、CLI、mod 的 `notify.push`
stage 共用同一发送层。

红线(防漂移护栏,同 model.py §6.1):
  - 只做**机制**:渠道分发 / 发送 / 固定次数 retry / dry-run / NotifyLog。消息**文案/模板/
    双语/收件人解析**全是上层(命令 / mod)策略,预渲染好的 `payload["text"]` 传进来。
  - 不 import 任何飞书 SDK、不引第三方 HTTP 库(只用 stdlib `subprocess`)。
  - **密钥永不进 argv / payload / 日志**:lark-cli 自己的 config 持 bot token(部署期
    `lark-cli config init`),本模块只调 CLI、绝不读写 app_secret。

约定(实测自 lark-im/references/lark-im-messages-send.md):
  - 私信:`lark-cli im +messages-send --user-id <open_id> --text <文本> --as bot`,
    `--as bot` 用 tenant_access_token、需 scope `im:message:send_as_bot`;返回
    `{"message_id":"om_...","chat_id":"oc_...","create_time":"..."}`。
  - @提及:正文里写 `<at user_id="ou_xxx">名</at>`(由上层拼好,本层不碰)。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover — 非 POSIX
    fcntl = None

from entity import ZeError  # 叶子直接 import(已由 sdk/_paths 注入路径;避免 barrel 循环)
from effect_context import assert_external_effect_allowed

# 收件人(open_id / 名册 key)字符白名单:即作 CLI flag 值,挡注入与异常字符。
_RECIPIENT_RE = re.compile(r"[A-Za-z0-9_@.\-]+")
_MESSAGE_ID_RE = re.compile(r"om_[A-Za-z0-9]+")
# amber service PATH 漏 ~/.npm-global/bin → which 兜底候选(见 reference_amber_service_path_omits_npm_global)。
_LARK_CLI_FALLBACKS = (
    "~/.npm-global/bin/lark-cli",
    "/usr/local/bin/lark-cli",
    "/opt/homebrew/bin/lark-cli",
    "/usr/bin/lark-cli",
)
NOTIFY_AUDIT_SCHEMA_VERSION = "1.0.0"

STATUS_SENT = "sent"
STATUS_DRY_RUN = "dry_run"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class NotifyResult:
    """一次发送的结果(成功/失败都返回,不抛;只在配置/校验类错误抛 ZeError)。"""

    ok: bool
    channel: str
    recipient: str
    status: str                 # sent | dry_run | failed
    message_id: str = ""
    error: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "channel": self.channel, "recipient": self.recipient,
            "status": self.status, "message_id": self.message_id,
            "error": self.error, "latency_ms": self.latency_ms,
        }


# ==============================================================
# NotifyLog(NDJSON 旁路,仿 auth.AuditLog;**不是 OpLog**)
# ==============================================================
# 每次发送尝试(含 dry-run / 失败)一行,供运维/AI 核「通知是否真发出」。
# 数据变更仍走 Operation(发送结果挂该 Operation 的 external 字段);这里只做聚合执行日志。

_NOTIFY_IO_LOCK = threading.RLock()


@contextmanager
def _exclusive_file_lock(path: Path):
    """跨进程 append 锁(NDJSON 单行 append 在 POSIX 基本原子,加锁保跨服务串行)。"""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _NOTIFY_IO_LOCK:
        with lock_path.open("a+", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def default_notify_log_path() -> Path:
    """默认落 ZE-system 身份目录(与 auth_audit 同处),不耦合 auth 模块。"""
    env = os.environ.get("ZE_NOTIFY_LOG_PATH")
    if env:
        return Path(env).expanduser().resolve()
    base = os.environ.get("ZE_SYSTEM_AUTH_DIR")
    if base:
        return (Path(base).expanduser() / "notify_audit.ndjson").resolve()
    return (
        Path.home() / ".claude" / "skills" / "ZE-skill-system"
        / "ZE-skill-hub-data" / "auth" / "notify_audit.ndjson"
    )


class NotifyLog:
    """外发通知审计 NDJSON。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser().resolve() if path else default_notify_log_path()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, *, channel: str, recipient: str, status: str,
               template: str = "", message_id: str = "", error: str = "",
               actor_user_id: str = "", tool_id: str = "", source: str = "server",
               **details: Any) -> dict[str, Any]:
        event = {
            "schema_version": NOTIFY_AUDIT_SCHEMA_VERSION,
            "ts": time.time(),
            "channel": channel,
            "recipient": recipient,
            "status": status,
            "template": template,
            "message_id": message_id,
            "error": error,
            "actor_user_id": actor_user_id,
            "tool_id": tool_id,
            "source": source,
            "details": details,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self._path):
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def read(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= max(1, limit):
                break
        return out


# ==============================================================
# retry(薄:固定次数 + 线性退避;瞬时 I/O 失败才重试,ZeError 不重试)
# ==============================================================

def _with_retry(fn: Callable[[], Any], *, retries: int, base_delay: float = 1.0) -> Any:
    last: Exception | None = None
    for attempt in range(max(1, retries + 1)):
        try:
            return fn()
        except ZeError:
            raise                                   # 配置/校验类错误不重试
        except Exception as e:                      # noqa: BLE001 — 瞬时发送失败才重试
            last = e
            if attempt < retries:
                time.sleep(base_delay * (attempt + 1))
    raise last if last else RuntimeError("notify retry exhausted")


def _cfg_bool(config: Any, key: str, default: bool) -> bool:
    raw = config.get(key, None) if config is not None else None
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# ==============================================================
# 发送主入口(按 channel 分发)
# ==============================================================

def notify(config: Any, *, channel: str = "feishu", recipient: str,
           payload: dict[str, Any] | None = None, dry_run: bool | None = None,
           timeout: int = 30, retries: int = 2,
           notify_log: "NotifyLog | None" = None,
           template: str = "", actor_user_id: str = "", tool_id: str = "",
           source: str = "server") -> NotifyResult:
    """发一条外发通知,返回 NotifyResult(发送失败不抛、记在 result;只校验/配置错才抛 ZeError)。

    config:ConfigRegistry(读 `ze.notify.*` / 凭证由 lark-cli 自身 config 持)。
    recipient:渠道收件人(feishu=open_id);payload["text"]=上层预渲染正文(含 @ 提及)。
    dry_run:None→读 `ze.notify.dry_run`(默认 True,安全:不显式开就不真发)。
    """
    assert_external_effect_allowed(f"notify.send:{channel}")
    payload = payload or {}
    text = str(payload.get("text") or "")
    recipient = str(recipient or "")
    if not recipient or not _RECIPIENT_RE.fullmatch(recipient):
        raise ZeError("validation_failed", "非法收件人", context={"recipient": recipient})
    if not text.strip():
        raise ZeError("validation_failed", "通知正文为空", context={"channel": channel})
    backend = _BACKENDS.get(channel)
    if backend is None:
        raise ZeError("validation_failed", f"未知通知渠道: {channel!r}",
                      context={"channel": channel, "allowed": list(_BACKENDS)})
    if dry_run is None:
        dry_run = _cfg_bool(config, "ze.notify.dry_run", True)

    def _record(res: NotifyResult) -> None:
        if notify_log is not None:
            try:
                notify_log.append(channel=res.channel, recipient=res.recipient,
                                  status=res.status, template=template,
                                  message_id=res.message_id, error=res.error,
                                  actor_user_id=actor_user_id, tool_id=tool_id, source=source)
            except Exception:                       # noqa: BLE001 — 日志失败不应阻断主流程
                pass

    if dry_run:
        res = NotifyResult(ok=True, channel=channel, recipient=recipient,
                           status=STATUS_DRY_RUN)
        _record(res)
        return res

    t0 = time.monotonic()
    try:
        out = _with_retry(lambda: backend(config, recipient, payload, timeout), retries=retries)
        res = NotifyResult(ok=True, channel=channel, recipient=recipient,
                           status=STATUS_SENT, message_id=str(out.get("message_id", "")),
                           latency_ms=int((time.monotonic() - t0) * 1000))
    except ZeError:
        raise                                       # 配置/校验错向上抛(让命令层显式处理)
    except Exception as e:                          # noqa: BLE001 — 发送失败 → 失败 result
        res = NotifyResult(ok=False, channel=channel, recipient=recipient,
                           status=STATUS_FAILED, error=str(e)[:300],
                           latency_ms=int((time.monotonic() - t0) * 1000))
    _record(res)
    return res


# ==============================================================
# Feishu 后端(backend #1):shell lark-cli bot 私信
# ==============================================================

def _resolve_lark_cli(config: Any) -> str:
    """定位 lark-cli:显式配置 > PATH > 常见目录兜底(amber PATH 漏 ~/.npm-global/bin)。"""
    explicit = str((config.get("ze.notify.lark_cli_bin", "") if config else "") or "")
    cand = ""
    if explicit:
        cand = str(Path(explicit).expanduser())
    if not cand:
        cand = shutil.which("lark-cli") or ""
    if not cand:
        for c in _LARK_CLI_FALLBACKS:
            p = Path(c).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                cand = str(p)
                break
    if not cand:
        raise ZeError("notify_failed",
                      "找不到 lark-cli(配 ze.notify.lark_cli_bin 或装到 PATH)", context={})
    if "\x00" in cand or "\n" in cand:
        raise ZeError("validation_failed", "非法 lark-cli 路径", context={})
    return cand


def _parse_message_id(stdout: str) -> dict[str, Any]:
    """从 lark-cli 输出取 message_id;先按 JSON 解,失败回退正则。返回 {} 也算成功(rc=0)。"""
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            mid = data.get("message_id") or (data.get("data") or {}).get("message_id")
            if mid:
                return {"message_id": str(mid)}
    except json.JSONDecodeError:
        pass
    m = _MESSAGE_ID_RE.search(text)
    return {"message_id": m.group(0)} if m else {}


def _feishu_send(config: Any, recipient: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    """lark-cli im +messages-send --user-id <open_id> --text <文本> --as bot。

    list 形式 subprocess(无 shell,元字符天然失效);app_secret 由 lark-cli 自身 config 持,
    本函数绝不触碰密钥。返回值含 message_id(可能为空但 rc=0 仍算成功)。
    """
    binary = _resolve_lark_cli(config)
    text = str(payload.get("text") or "")
    args = [binary, "im", "+messages-send", "--user-id", recipient, "--text", text, "--as", "bot"]
    idem = payload.get("idempotency_key")
    if idem:
        args += ["--idempotency-key", str(idem)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise ZeError("notify_failed", f"找不到 lark-cli 可执行: {binary!r}",
                      context={"binary": binary}) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"lark-cli 发送超时({timeout}s)") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"lark-cli 退出码 {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:300]}")
    return _parse_message_id(proc.stdout)


# 可插拔后端注册表:日后 email/webhook 只加函数 + 注册一行,notify() 与调用方零改。
_BACKENDS: dict[str, Callable[[Any, str, dict, int], dict]] = {
    "feishu": _feishu_send,
}


def register_backend(channel: str, sender: Callable[[Any, str, dict, int], dict]) -> None:
    """注册自定义通知后端(channel→sender(config, recipient, payload, timeout)->{message_id})。"""
    _BACKENDS[channel] = sender


def available_channels() -> list[str]:
    return sorted(_BACKENDS)
