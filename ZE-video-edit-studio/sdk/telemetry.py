"""telemetry — 旁路「使用埋点」发射器(usage telemetry,push 模型)。

框架层旁路遥测:工具在动作发生时把**聚合用**事件推给中心采集器(ZE-skill-system
的 /api/usage/ingest),供其统计「用户数 / 会话 / 时长 / 主要功能」。

设计红线(埋点永不影响工具本身):
  - `emit()` 只入有界队列、立即返回(非阻塞);后台守护线程批量 flush。
  - 队列满即丢、网络/序列化异常全吞 —— 任何遥测故障都不得冒泡到主流程。
  - 采集器地址未配置(env ZE_USAGE_COLLECTOR_URL 空)= 整体 no-op,零外连、零线程。
  - 不进 oplog、不产生 Operation —— 执行元数据与撤销/审计通道解耦(editor-arch)。

隐私(仅聚合):事件只带 `anon_user = sha256(user_id)[:16]`(稳定、不可逆),
采集器据此去重计数,但不存/不露用户名。

契约 `usage-event.v1`(与 ZE-skill-system/modules/usage-telemetry-contract.md 对齐):
  { schema:"usage-event.v1", tool_id, event_type:"session_start"|"feature_use",
    anon_user, command_id?, feature_label?, ts }

注:本模块**不进 sdk.__init__ 的 __all__ 公开契约**(它是框架内部管线,非 mod 面 API,
也避免改动 core 层 checksum);server.py 直接 `from sdk.telemetry import ...`。整个 sdk/
目录随 scaffold/skill_sync 整树复制,故下游 v3 工具自动获得发射器。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Callable, Optional

# 注:不用 stdlib `queue` —— 平铺布局里 01-core/.../05-concurrency/queue.py 会在
# _paths 注入叶子后遮蔽 stdlib(`import queue` 取到错的模块)。改用 collections.deque
# (maxlen 自动丢最旧、append/popleft 线程安全),既避坑又自带背压语义。

USAGE_EVENT_SCHEMA = "usage-event.v1"
ENV_COLLECTOR_URL = "ZE_USAGE_COLLECTOR_URL"

_DEFAULT_FLUSH_INTERVAL = 5.0     # 秒;后台线程每隔此间隔 flush 一次
_DEFAULT_BATCH = 50               # 单次 POST 最多带多少条
_DEFAULT_MAX_QUEUE = 2000         # 队列上限;满则丢弃(背压保护)
_POST_TIMEOUT = 3.0               # 秒;采集器无响应时快速放弃

_FRONTMATTER_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


def anon_user(user_id: str) -> str:
    """user_id → 稳定、不可逆的匿名 id(sha256 前 16 hex)。空 → 'anon'。"""
    if not user_id:
        return "anon"
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def read_tool_id(root: Path | str) -> str:
    """从 skill 根的 SKILL.md frontmatter 读 `name:` 作 tool_id;读不到回落目录名。"""
    root = Path(root)
    skill_md = root / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
        m = _FRONTMATTER_NAME_RE.search(text)
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return root.name


# 事件发送函数类型:接收一批事件 dict,负责投递(或抛异常由 _flush 吞掉)。
Transport = Callable[[list[dict]], None]


class UsageEmitter:
    """埋点发射器:非阻塞入队 + 后台批量 flush。url 空 = 全程 no-op。

    `_transport` 可注入(测试用),默认走 urllib POST 到 {collector_url}/api/usage/ingest。
    """

    def __init__(
        self,
        *,
        tool_id: str,
        collector_url: Optional[str] = None,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL,
        batch_size: int = _DEFAULT_BATCH,
        max_queue: int = _DEFAULT_MAX_QUEUE,
        transport: Optional[Transport] = None,
    ) -> None:
        self.tool_id = tool_id
        self.collector_url = (collector_url or "").strip().rstrip("/")
        self.enabled = bool(self.collector_url) or transport is not None
        self._flush_interval = flush_interval
        self._batch_size = max(1, batch_size)
        self._buf: deque = deque(maxlen=max(1, max_queue))   # 满 → 自动丢最旧
        self._lock = threading.Lock()
        self._transport: Transport = transport or self._post
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 仅在「真要发」时才起线程:url 空且无注入 transport → 零线程零外连。
        if self.enabled:
            self._thread = threading.Thread(
                target=self._loop, name="ze-usage-emitter", daemon=True,
            )
            self._thread.start()

    # ---- 对外 API ----

    def emit(
        self,
        event_type: str,
        *,
        anon: str,
        command_id: Optional[str] = None,
        feature_label: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> None:
        """构造一条 usage-event.v1 并入队(非阻塞)。no-op 模式直接返回。"""
        if not self.enabled:
            return
        event = {
            "schema": USAGE_EVENT_SCHEMA,
            "tool_id": self.tool_id,
            "event_type": event_type,
            "anon_user": anon,
            "ts": ts if ts is not None else time.time(),
        }
        if command_id is not None:
            event["command_id"] = command_id
        if feature_label is not None:
            event["feature_label"] = feature_label
        with self._lock:
            self._buf.append(event)  # 满则 deque(maxlen) 自动丢最旧,绝不阻塞主流程

    def pending(self) -> int:
        """当前缓冲区待发条数(测试 / 观测用)。"""
        with self._lock:
            return len(self._buf)

    def flush_now(self) -> None:
        """同步把当前缓冲区全部发出(测试 / 优雅关闭用)。异常吞掉。"""
        self._flush(self._drain(None))

    def close(self) -> None:
        """停后台线程并尽力发完剩余事件。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.flush_now()

    # ---- 内部 ----

    def _drain(self, max_n: Optional[int]) -> list[dict]:
        """从缓冲区头部取最多 max_n 条(None = 全部)。"""
        batch: list[dict] = []
        with self._lock:
            while self._buf and (max_n is None or len(batch) < max_n):
                batch.append(self._buf.popleft())
        return batch

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._flush_interval)
            self._flush(self._drain(self._batch_size))
        self._flush(self._drain(None))  # 收尾:发完剩余

    def _flush(self, batch: list[dict]) -> None:
        if not batch:
            return
        try:
            self._transport(batch)
        except Exception:
            pass  # 遥测失败不影响工具:吞掉(含网络/序列化/采集器宕)

    def _post(self, batch: list[dict]) -> None:
        data = json.dumps({"events": batch}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.collector_url + "/api/usage/ingest",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=_POST_TIMEOUT).read()


# 进程内禁用单例:server.py 在未配置采集器时用它,emit 全是 no-op。
NULL_EMITTER = UsageEmitter(tool_id="", collector_url=None)
