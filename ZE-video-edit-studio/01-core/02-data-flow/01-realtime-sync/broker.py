"""Realtime Sync — RealtimeBroker (进程内 topic-based pub/sub).

完整 SPEC 见同目录 SPEC.md.

已实装 (无跨叶子依赖):
  - RealtimeBroker(replay_buffer_size=256, transport=None)
      publish_change(payload) → seq
      subscribe(entity_type, entity_id, doc_id, callback, since_seq) → Subscription
      stats() — {n_subscribers, n_messages, last_seq, topics}
  - Subscription 句柄 + unsubscribe()
  - Topic 格式: entity:<type>:<id> / entity:<type>:* / entity:*:* / doc:<doc_id>
  - replay buffer (collections.deque maxlen=replay_buffer_size)
  - 默认 InMemoryTransport; transport=None 时自动用

设计 (见 SPEC §3-§6):
  - 不修改 Document / OpLog (观察者模式)
  - payload 严格按 _protocol/CHANGE-FLOW §4 形态
  - callback 抛错 broker catch + 继续 (不污染别人)
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 平铺: 注入 _protocol 兄弟目录 (复用 change_set 协议常量)
_HERE = Path(__file__).resolve().parent
_PROTOCOL_DIR = _HERE.parent.parent / "_protocol"
if str(_PROTOCOL_DIR) not in sys.path:
    sys.path.insert(0, str(_PROTOCOL_DIR))

from transport import InMemoryTransport, Transport


# ==============================================================
# Subscription 句柄
# ==============================================================

@dataclass
class _SubRecord:
    """broker 内部用: 记一个 subscription 的过滤条件."""
    sub_id: str
    entity_type: str | None
    entity_id: str | None
    doc_id: str | None
    # replay 与 live publish 都在 broker 主锁内按全局 seq 进入该队列；锁外只负责
    # drain。这样 subscribe 先注册、后锁外 replay 时，不会出现 seq=11 抢在
    # replay 的 seq=1..10 前面送达。
    pending: deque[dict] = field(default_factory=deque)
    delivering: bool = False
    replay_blocked: bool = False
    observed_seq: int = 0
    settled_seq: int = 0


@dataclass
class Subscription:
    """订阅句柄. 持有就能 unsubscribe.

    支持 context manager: 进入时是自身, 退出时自动 unsubscribe; 防泄漏.
    """
    sub_id: str
    broker: "RealtimeBroker"
    requested_since_seq: int = 0
    oldest_buffered_seq: int = 0
    last_seq_at_subscribe: int = 0
    replay_gap: bool = False
    gap_reason: str = ""

    def unsubscribe(self) -> None:
        self.broker._unregister(self.sub_id)

    def wait_until_delivered(self, seq: int, timeout: float = 1.0) -> bool:
        """Wait until this subscription has drained every matched event <= seq."""
        return self.broker._wait_until_settled(self.sub_id, seq, timeout)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.unsubscribe()


# ==============================================================
# RealtimeBroker
# ==============================================================

class RealtimeBroker:
    """进程内 topic-based pub/sub.

    publish 按 changes 自动分发; subscribe 按过滤条件订阅.
    transport 抽象给未来跨设备同步留 hook.
    """

    def __init__(
        self,
        *,
        replay_buffer_size: int = 256,
        transport: Transport | None = None,
    ) -> None:
        if replay_buffer_size <= 0:
            raise ValueError("replay_buffer_size must be > 0")
        self._buffer: deque[tuple[int, dict]] = deque(maxlen=replay_buffer_size)
        self._subs: dict[str, _SubRecord] = {}
        self._transport: Transport = transport or InMemoryTransport()
        self._seq = 0
        # Broker seq is process-local.  A random incarnation id prevents a
        # restart (cursor 100 -> 0 in the same project generation) from looking
        # like a valid monotonic continuation to clients.
        self._stream_id = uuid.uuid4().hex
        self._n_messages = 0
        # 线程安全锁: 保护 _subs / _buffer / _seq / _n_messages 的并发访问.
        # SPEC §2 说 "in-process" 不限单线程; asyncio + 后台线程并存时这个锁防丢/防重.
        self._lock = threading.RLock()
        self._delivery_changed = threading.Condition(self._lock)
        # AP-R3 防御: 同一线程的 callback 在 publish 内回头再调 publish 会死循环;
        # 用 thread-local 计数检测重入, 第二次抛错以暴露 bug.
        self._publishing = threading.local()

    # ---- publish ----

    def publish_change(self, payload: dict) -> int:
        """Broadcast 一个 entity_change payload.

        - seq += 1
        - 写进 replay buffer
        - 对每个 change: 分发到匹配 topics 的 subscribers
        - 返回 seq
        """
        if not isinstance(payload, dict):
            raise TypeError(f"payload must be dict, got {type(payload).__name__}")
        # 不强校 payload 形态 (caller 通过 _protocol/to_broadcast_payload 构造);
        # SPEC §10 红线: payload 应符合 §4 形态

        # 重入检测 (AP-R3): 同一线程在 callback 里再调 publish_change → 抛错
        if getattr(self._publishing, "active", False):
            raise RuntimeError(
                "RealtimeBroker.publish_change re-entered inside a subscriber "
                "callback — 这是 AP-R3 反模式 (callback 触发 dispatch → publish 循环). "
                "subscriber 应只渲染/记录, 不应在 callback 里执行命令."
            )
        self._publishing.active = True
        try:
            with self._lock:
                self._seq += 1
                seq = self._seq
                wire_payload = dict(payload)
                wire_payload["seq"] = seq
                self._buffer.append((seq, wire_payload))
                self._n_messages += 1

                doc_id = wire_payload.get("doc_id")
                changes = wire_payload.get("changes") or []

                # 收集匹配此 payload 的所有 sub_ids (持锁; 一致性快照)
                matched_subs: set[str] = set()
                for change in changes:
                    et = change.get("entity_type")
                    eid = change.get("entity_id")
                    for sub_id, sub in self._subs.items():
                        if self._sub_matches(sub, et, eid, doc_id):
                            matched_subs.add(sub_id)
                if not changes:
                    # 空 changes 仍向 "听一切" / doc_id 订阅发送
                    for sub_id, sub in self._subs.items():
                        if self._sub_matches(sub, None, None, doc_id):
                            matched_subs.add(sub_id)

                # 入每订阅队列必须和 seq 分配、replay snapshot 同在这把锁内。
                # 实际 callback 仍在锁外执行，慢订阅者不会阻塞 broker 主锁。
                for sub_id, sub in self._subs.items():
                    sub.observed_seq = seq
                    if sub_id in matched_subs and not sub.replay_blocked:
                        sub.pending.append(wire_payload)
                    elif not sub.pending and not sub.delivering:
                        sub.settled_seq = seq
                self._delivery_changed.notify_all()

            # transport.deliver 不持锁 (callback 可能慢; 持锁会阻塞其他 publish)
            for sub_id in matched_subs:
                self._drain_sub(sub_id)

            return seq
        finally:
            self._publishing.active = False

    # ---- subscribe ----

    def subscribe(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        doc_id: str | None = None,
        callback: Callable[[dict], None],
        since_seq: int = 0,
    ) -> Subscription:
        """订阅. entity_type / entity_id / doc_id 都是过滤;
        None 表示该维度不过滤 (匹配任意).

        since_seq > 0 时先 replay buffer 里 seq >= since_seq 的 payload (符合过滤),
        再开始接收新 publish.
        """
        sub_id = uuid.uuid4().hex
        sub = _SubRecord(
            sub_id=sub_id,
            entity_type=entity_type,
            entity_id=entity_id,
            doc_id=doc_id,
        )
        with self._lock:
            oldest_seq = self._buffer[0][0] if self._buffer else 0
            last_seq = self._seq
            replay_gap = False
            gap_reason = ""
            if since_seq > 0:
                if since_seq > last_seq + 1:
                    replay_gap = True
                    gap_reason = "cursor_ahead"
                elif since_seq <= last_seq and (
                    not self._buffer or since_seq < oldest_seq
                ):
                    replay_gap = True
                    gap_reason = "replay_buffer_gap"

            sub.replay_blocked = replay_gap
            sub.observed_seq = last_seq
            self._subs[sub_id] = sub
            self._transport.register_subscriber(sub_id, callback)

            # Gap 时绝不交付一个看似连续的后缀；caller 必须先取 canonical
            # snapshot。无 gap 时 replay 与后续 live publish 共用 pending 队列，
            # 因此它们的送达顺序与 broker seq 严格一致。
            if since_seq > 0 and not replay_gap:
                for seq, payload in self._buffer:
                    if seq < since_seq:
                        continue
                    doc_id_payload = payload.get("doc_id")
                    changes = payload.get("changes") or []
                    matched = (
                        self._sub_matches(sub, None, None, doc_id_payload)
                        if not changes else any(
                            self._sub_matches(
                                sub, ch.get("entity_type"),
                                ch.get("entity_id"), doc_id_payload,
                            )
                            for ch in changes
                        )
                    )
                    if matched:
                        sub.pending.append(payload)
            if not sub.pending:
                sub.settled_seq = last_seq

        self._drain_sub(sub_id)
        return Subscription(
            sub_id=sub_id,
            broker=self,
            requested_since_seq=since_seq,
            oldest_buffered_seq=oldest_seq,
            last_seq_at_subscribe=last_seq,
            replay_gap=replay_gap,
            gap_reason=gap_reason,
        )

    def _drain_sub(self, sub_id: str) -> None:
        """Deliver one subscriber's queued payloads in broker-seq order.

        Only one thread may drain a subscriber.  Other publishers append under
        the broker lock and return; the active drainer observes their payloads
        before atomically clearing ``delivering``.
        """
        with self._lock:
            sub = self._subs.get(sub_id)
            if sub is None or sub.delivering:
                return
            sub.delivering = True
        while True:
            with self._lock:
                sub = self._subs.get(sub_id)
                if sub is None:
                    return
                if not sub.pending:
                    sub.delivering = False
                    sub.settled_seq = sub.observed_seq
                    self._delivery_changed.notify_all()
                    return
                payload = sub.pending.popleft()
            try:
                self._transport.deliver(sub_id, payload)
            except Exception:
                # Transport contract mirrors callback isolation: one broken
                # delivery must not strand the ordered queue forever.
                continue

    # ---- unsubscribe ----

    def _unregister(self, sub_id: str) -> None:
        """Subscription.unsubscribe 内部回调."""
        with self._lock:
            self._subs.pop(sub_id, None)
            self._transport.unregister(sub_id)
            self._delivery_changed.notify_all()

    def _wait_until_settled(self, sub_id: str, seq: int, timeout: float) -> bool:
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ValueError("seq must be a non-negative integer")
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._delivery_changed:
            while True:
                sub = self._subs.get(sub_id)
                if sub is None:
                    return False
                if sub.settled_seq >= seq:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._delivery_changed.wait(remaining)

    # ---- 状态查询 ----

    def stats(self) -> dict[str, Any]:
        """快照式状态; 'oldest_buffered_seq' 让 caller 判断 since_seq 是否丢消息."""
        with self._lock:
            topics = sorted({
                self._topic_of(sub) for sub in self._subs.values()
            })
            oldest_seq = self._buffer[0][0] if self._buffer else 0
            return {
                "n_subscribers": len(self._subs),
                "n_messages": self._n_messages,
                "last_seq": self._seq,
                "stream_id": self._stream_id,
                "oldest_buffered_seq": oldest_seq,
                "buffer_size": len(self._buffer),
                "topics": topics,
            }

    # ---- 内部辅助 ----

    @staticmethod
    def _sub_matches(
        sub: _SubRecord,
        entity_type: str | None,
        entity_id: str | None,
        doc_id: str | None,
    ) -> bool:
        """检查一个 change (entity_type/entity_id/doc_id) 是否匹配 sub 过滤."""
        if sub.doc_id is not None:
            if doc_id != sub.doc_id:
                return False
        if sub.entity_type is not None:
            if entity_type != sub.entity_type:
                return False
        if sub.entity_id is not None:
            if entity_id != sub.entity_id:
                return False
        return True

    @staticmethod
    def _topic_of(sub: _SubRecord) -> str:
        """给 stats() 用的可读 topic 标签."""
        if sub.doc_id is not None:
            return f"doc:{sub.doc_id}"
        et = sub.entity_type or "*"
        eid = sub.entity_id or "*"
        return f"entity:{et}:{eid}"


__all__ = [
    "RealtimeBroker",
    "Subscription",
]
