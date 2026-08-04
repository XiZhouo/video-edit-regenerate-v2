"""cli_login.py — 服务端驱动的 Codex / Claude 设备登录。

让用户在**自己的浏览器**登录**自己的** Codex/Claude 账号,凭据落进各自隔离目录
(浮层 AI 用各人订阅额度、互不串号)。傻瓜流程,不用碰任何文件:

  Codex:  `codex login --device-auth`(普通子进程,无需 TTY)→ 打印固定授权链接
          https://auth.openai.com/codex/device + 一次性验证码 → 用户在浏览器批准 →
          CLI 后台轮询到批准 → 写 CODEX_HOME/auth.json。
  Claude: `claude setup-token`(Ink TUI,须伪终端 PTY)→ 打印 claude.com 授权链接 →
          用户浏览器登录后网页给一串 code → 回填进 PTY → 写 CLAUDE_CONFIG_DIR/.credentials.json。

会话短命(≤15 分钟)、临时目录登录、**成功才建正式绑定**(失败不留垃圾)。进程/临时目录
在完成/出错/超时/取消时一律清理,不泄漏。

红线:只做 Codex/Claude 两个 CLI provider 的登录编排;凭据存储/隔离目录复用 credentials.py。
"""
from __future__ import annotations

import atexit
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from credentials import (  # noqa: E402  同叶子直接 import(路径已由 sdk/_paths 注入)
    PROVIDER_CODEX_CLI, PROVIDER_CLAUDE_CLI, CLI_PROVIDERS, CLI_HOME_AUTH_FILE,
    provision_cli_home, _clean_owner,
)
from entity import ZeError  # noqa: E402

# ---- 状态 ----
STATUS_PENDING = "pending"              # Codex:已给链接+验证码,等用户在浏览器批准(前端轮询)
STATUS_AWAITING_CODE = "awaiting_code"  # Claude:已给链接,等用户把网页 code 粘回来
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"

SESSION_TTL_SEC = 15 * 60               # 与设备码 15 分钟有效期对齐
_START_WAIT_SEC = 20                    # 起进程后等它打印授权链接的最长秒数

# 抓取正则
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CODEX_URL = "https://auth.openai.com/codex/device"
_CODEX_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,8}\b")   # 如 U87F-0UWSR(4-5)
_CLAUDE_URL_START = "https://claude.com/"
_URL_QS_RE = re.compile(r"(https?://\S+?)\?\S+")   # URL 上的 query string 可能带 token/一次性码


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _safe_tail(buf: str, n: int = 400) -> str:
    """诊断尾巴回显给客户端/日志前脱敏:抹掉设备验证码与 URL query string(可能含 token/一次性码),
    再截末 n 字。over-redact 无害——错误文本只用于人肉排障。"""
    s = _strip_ansi(buf)
    s = _CODEX_CODE_RE.sub("[code]", s)     # 设备验证码
    s = _URL_QS_RE.sub(r"\1?[redacted]", s)  # 授权 URL 的敏感 query string
    return s[-n:]


@dataclass
class _Session:
    login_id: str
    user_id: str
    provider: str
    status: str = STATUS_PENDING
    verify_url: str = ""
    user_code: str = ""
    needs_code_paste: bool = False
    error: str = ""
    cred_id: str = ""
    tmp_home: str = ""
    created_at: float = field(default_factory=time.time)
    # 运行态(不外泄)
    _proc: subprocess.Popen | None = None
    _pty_fd: int = -1
    _pid: int = 0
    _buf: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def public(self) -> dict[str, Any]:
        return {
            "login_id": self.login_id,
            "provider": "codex" if self.provider == PROVIDER_CODEX_CLI else "claude",
            "status": self.status,
            "verify_url": self.verify_url,
            "user_code": self.user_code,
            "needs_code_paste": self.needs_code_paste,
            "error": self.error,
        }


class CliLoginManager:
    """进程内单例:管理进行中的设备登录会话。"""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    # ---- 对外 API ----

    def start(self, user_id: str, provider: str, *, codex_bin: str = "codex",
              claude_bin: str = "claude") -> dict[str, Any]:
        uid = _clean_owner(user_id)
        prov = str(provider or "").strip().lower()
        if prov in ("codex", "codex_cli_profile"):
            prov = PROVIDER_CODEX_CLI
        elif prov in ("claude", "claude_cli_profile"):
            prov = PROVIDER_CLAUDE_CLI
        if prov not in CLI_PROVIDERS:
            raise ZeError("validation_failed", "只支持 codex / claude 登录",
                          context={"provider": provider})
        self._sweep_expired()
        self._cancel_user_pending(uid)          # 一个用户同时只留一个进行中登录

        sess = _Session(login_id="lg_" + secrets.token_hex(8), user_id=uid, provider=prov)
        sess.tmp_home = tempfile.mkdtemp(prefix="ze-cli-login-")
        binary = codex_bin if prov == PROVIDER_CODEX_CLI else claude_bin
        if not binary or "\x00" in binary or "\n" in binary:
            self._discard(sess)
            raise ZeError("validation_failed", "非法 CLI 可执行配置", context={})
        try:
            if prov == PROVIDER_CODEX_CLI:
                self._start_codex(sess, binary)
            else:
                self._start_claude(sess, binary)
        except ZeError:
            self._discard(sess)
            raise
        except Exception as e:  # noqa: BLE001
            self._discard(sess)
            raise ZeError("model_call_failed", f"启动登录失败: {e}", context={}) from e

        with self._lock:
            self._sessions[sess.login_id] = sess
        return sess.public()

    def poll(self, user_id: str, login_id: str, store: Any) -> dict[str, Any]:
        sess = self._require(user_id, login_id)
        if sess.provider == PROVIDER_CODEX_CLI and sess.status == STATUS_PENDING:
            self._advance_codex(sess, store)
        elif sess.status == STATUS_AWAITING_CODE:
            pass  # Claude 等用户提交 code,poll 只回当前态
        self._expire_if_stale(sess)
        return sess.public()

    def submit_code(self, user_id: str, login_id: str, code: str, store: Any) -> dict[str, Any]:
        sess = self._require(user_id, login_id)
        if sess.provider != PROVIDER_CLAUDE_CLI:
            raise ZeError("validation_failed", "该登录不需要粘贴 code", context={})
        if sess.status != STATUS_AWAITING_CODE:
            return sess.public()
        code = str(code or "").strip()
        if not code or "\n" in code or "\r" in code or len(code) > 512:
            raise ZeError("validation_failed", "无效的授权 code", context={})
        self._advance_claude(sess, code, store)
        return sess.public()

    def cancel(self, user_id: str, login_id: str) -> dict[str, Any]:
        sess = self._require(user_id, login_id)
        self._discard(sess)
        with self._lock:
            self._sessions.pop(login_id, None)
        return {"ok": True}

    # ---- Codex(设备码,无 TTY)----

    def _start_codex(self, sess: _Session, binary: str) -> None:
        env = {**os.environ, "CODEX_HOME": sess.tmp_home}
        proc = subprocess.Popen(
            [binary, "login", "--device-auth"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        sess._proc = proc
        threading.Thread(target=self._reader_codex, args=(sess,), daemon=True).start()
        # 等它打印链接+验证码
        deadline = time.time() + _START_WAIT_SEC
        while time.time() < deadline:
            with sess._lock:
                if sess.verify_url and sess.user_code:
                    return
                if proc.poll() is not None:  # 进程早退=失败
                    raise ZeError("model_call_failed",
                                  f"codex 登录进程异常退出: {_safe_tail(sess._buf)}",
                                  context={})
            time.sleep(0.3)
        raise ZeError("model_call_failed", "codex 登录未在预期时间内给出授权链接", context={})

    def _reader_codex(self, sess: _Session) -> None:
        proc = sess._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            clean = _strip_ansi(line)
            with sess._lock:
                sess._buf += clean
                if _CODEX_URL in clean and not sess.verify_url:
                    sess.verify_url = _CODEX_URL
                if not sess.user_code:
                    m = _CODEX_CODE_RE.search(clean)
                    if m:
                        sess.user_code = m.group(0)

    def _advance_codex(self, sess: _Session, store: Any) -> None:
        proc = sess._proc
        if proc is None:
            return
        rc = proc.poll()
        if rc is None:
            return  # 还在等用户批准
        # 进程结束:成功=exit0 且 auth.json 落盘
        auth = Path(sess.tmp_home) / CLI_HOME_AUTH_FILE[PROVIDER_CODEX_CLI]
        if rc == 0 and auth.is_file():
            self._finalize(sess, store)
        else:
            with sess._lock:
                sess.status = STATUS_ERROR
                sess.error = f"codex 登录失败(退出码 {rc}): {_safe_tail(sess._buf)}"
            self._cleanup_proc(sess)

    # ---- Claude(须 PTY,回填 code)----

    def _start_claude(self, sess: _Session, binary: str) -> None:
        import pty  # 仅 POSIX;lazy import
        env = {**os.environ, "CLAUDE_CONFIG_DIR": sess.tmp_home,
               "BROWSER": "/usr/bin/true", "TERM": "xterm",
               "PATH": os.environ.get("PATH", "")}
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.execvpe(binary, [binary, "setup-token"], env)
            except Exception:
                os._exit(127)
        sess._pid = pid
        sess._pty_fd = fd
        threading.Thread(target=self._reader_claude, args=(sess,), daemon=True).start()
        deadline = time.time() + _START_WAIT_SEC
        while time.time() < deadline:
            with sess._lock:
                if sess.verify_url:
                    sess.status = STATUS_AWAITING_CODE
                    sess.needs_code_paste = True
                    return
            if not self._alive(sess):
                raise ZeError("model_call_failed",
                              f"claude 登录进程异常退出: {_safe_tail(sess._buf)}",
                              context={})
            time.sleep(0.3)
        raise ZeError("model_call_failed", "claude 登录未在预期时间内给出授权链接", context={})

    def _reader_claude(self, sess: _Session) -> None:
        import select
        fd = sess._pty_fd
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 1.0)
            except (OSError, ValueError):
                break
            if not r:
                if not self._alive(sess):
                    break
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            clean = _strip_ansi(data.decode("utf-8", "replace"))
            with sess._lock:
                sess._buf += clean
                if not sess.verify_url and _CLAUDE_URL_START in sess._buf:
                    sess.verify_url = self._extract_claude_url(sess._buf)

    @staticmethod
    def _extract_claude_url(buf: str) -> str:
        # TUI 会把长 URL 按终端宽度硬折行;从 https://claude.com 起截到 code 提示/空行前,
        # 去掉内部空白拼回完整 URL(URL 本身无空白)。NOTE(amber-verify):折行拼接须部署机微调。
        idx = buf.find(_CLAUDE_URL_START)
        if idx < 0:
            return ""
        tail = buf[idx:]
        end = re.search(r"\n\s*\n|Paste|paste|>", tail)
        seg = tail[: end.start()] if end else tail[:600]
        url = re.sub(r"\s+", "", seg)
        return url if url.startswith("https://claude.com/") and len(url) > 40 else ""

    def _advance_claude(self, sess: _Session, code: str, store: Any) -> None:
        fd = sess._pty_fd
        try:
            os.write(fd, (code + "\r").encode("utf-8"))
        except OSError as e:
            with sess._lock:
                sess.status = STATUS_ERROR
                sess.error = f"回填 code 失败: {e}"
            self._cleanup_proc(sess)
            return
        # 等进程完成写凭据(≤60s)
        auth = Path(sess.tmp_home) / CLI_HOME_AUTH_FILE[PROVIDER_CLAUDE_CLI]
        deadline = time.time() + 60
        while time.time() < deadline:
            if auth.is_file() and not self._alive(sess):
                self._finalize(sess, store)
                return
            if not self._alive(sess):
                break
            time.sleep(0.5)
        if auth.is_file():
            self._finalize(sess, store)
            return
        with sess._lock:
            sess.status = STATUS_ERROR
            sess.error = f"claude 登录未完成: {_safe_tail(sess._buf)}"
        self._cleanup_proc(sess)

    # ---- 成功收尾:建正式绑定 ----

    def _finalize(self, sess: _Session, store: Any) -> None:
        auth_name = CLI_HOME_AUTH_FILE[sess.provider]
        src = Path(sess.tmp_home) / auth_name
        try:
            content = src.read_text(encoding="utf-8")
            json.loads(content)  # 校验是 JSON
        except Exception as e:  # noqa: BLE001
            with sess._lock:
                sess.status = STATUS_ERROR
                sess.error = f"登录凭据无效: {e}"
            self._cleanup_proc(sess)
            return
        label = self._auto_label(store, sess.user_id, sess.provider, content)
        rec = store.upsert_for_user(sess.user_id, provider=sess.provider, label=label)
        home = provision_cli_home(sess.user_id, sess.provider, rec.id)
        fd = os.open(home / auth_name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        store.upsert_for_user(sess.user_id, credential_id=rec.id, provider=sess.provider,
                              label=label, metadata={"home_dir": str(home)})
        with sess._lock:
            sess.status = STATUS_SUCCESS
            sess.cred_id = rec.id
        self._cleanup_proc(sess)
        shutil.rmtree(sess.tmp_home, ignore_errors=True)

    @staticmethod
    def _auto_label(store: Any, user_id: str, provider: str, content: str) -> str:
        friendly = "Codex" if provider == PROVIDER_CODEX_CLI else "Claude"
        suffix = ""
        try:
            d = json.loads(content)
            if provider == PROVIDER_CODEX_CLI:
                acct = str(((d.get("tokens") or {}).get("account_id")) or "")
                suffix = f" ·{acct[:6]}" if acct else ""
            else:
                sub = str((d.get("claudeAiOauth") or {}).get("subscriptionType") or "")
                suffix = f" ·{sub}" if sub else ""
        except Exception:  # noqa: BLE001
            pass
        n = sum(1 for r in store.list_for_user(user_id) if r.provider == provider) + 1
        return f"{friendly}{suffix}" if suffix else f"{friendly} #{n}"

    # ---- 内务 ----

    def _require(self, user_id: str, login_id: str) -> _Session:
        uid = _clean_owner(user_id)
        with self._lock:
            sess = self._sessions.get(login_id)
        if sess is None or sess.user_id != uid:
            raise ZeError("not_found", "登录会话不存在或已过期", context={})
        return sess

    def _expire_if_stale(self, sess: _Session) -> None:
        if sess.status in (STATUS_SUCCESS, STATUS_ERROR):
            return
        if time.time() - sess.created_at > SESSION_TTL_SEC:
            with sess._lock:
                sess.status = STATUS_ERROR
                sess.error = "登录超时(15 分钟未完成),请重试"
            self._discard(sess)

    def _sweep_expired(self) -> None:
        now = time.time()
        with self._lock:
            stale = [s for s in self._sessions.values()
                     if s.status in (STATUS_SUCCESS, STATUS_ERROR)
                     or now - s.created_at > SESSION_TTL_SEC + 60]
        for s in stale:
            if s.status not in (STATUS_SUCCESS,):
                self._cleanup_proc(s)
                shutil.rmtree(s.tmp_home, ignore_errors=True)
            with self._lock:
                self._sessions.pop(s.login_id, None)

    def _cancel_user_pending(self, uid: str) -> None:
        with self._lock:
            olds = [s for s in self._sessions.values()
                    if s.user_id == uid and s.status in (STATUS_PENDING, STATUS_AWAITING_CODE)]
        for s in olds:
            self._discard(s)
            with self._lock:
                self._sessions.pop(s.login_id, None)

    def _alive(self, sess: _Session) -> bool:
        if sess._proc is not None:
            return sess._proc.poll() is None
        if sess._pid:
            try:
                os.kill(sess._pid, 0)
                return True
            except OSError:
                return False
        return False

    def _cleanup_proc(self, sess: _Session) -> None:
        if sess._proc is not None:
            try:
                sess._proc.terminate()
                sess._proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    sess._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            sess._proc = None
        if sess._pid:
            try:
                os.kill(sess._pid, signal.SIGKILL)
            except OSError:
                pass
            sess._pid = 0
        if sess._pty_fd >= 0:
            try:
                os.close(sess._pty_fd)
            except OSError:
                pass
            sess._pty_fd = -1

    def _discard(self, sess: _Session) -> None:
        self._cleanup_proc(sess)
        if sess.tmp_home and sess.status != STATUS_SUCCESS:
            shutil.rmtree(sess.tmp_home, ignore_errors=True)

    def shutdown_all(self) -> None:
        """server 关停/重启时清扫所有进行中登录:杀子进程 + 关 PTY + 删临时目录。
        防「登录进行中恰逢 redeploy → codex/claude 子进程与 PTY fd 在 amber 上无限泄漏」。
        由 server 生命周期 shutdown 钩子调用,并经 atexit 兜底(正常退出必触发)。"""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            try:
                self._cleanup_proc(s)
            except Exception:  # noqa: BLE001
                pass
            if s.tmp_home and s.status != STATUS_SUCCESS:
                shutil.rmtree(s.tmp_home, ignore_errors=True)


# 进程内单例(server 直接用)
LOGIN_MANAGER = CliLoginManager()
atexit.register(LOGIN_MANAGER.shutdown_all)   # 兜底:正常退出/uvicorn 优雅关停都清扫,不泄漏子进程

__all__ = ["CliLoginManager", "LOGIN_MANAGER",
           "STATUS_PENDING", "STATUS_AWAITING_CODE", "STATUS_SUCCESS", "STATUS_ERROR"]
