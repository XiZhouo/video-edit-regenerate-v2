"""Transport — broker 把 payload 送到 subscriber 的传输层抽象.

完整 SPEC 见同目录 SPEC.md §2.

已实装:
  - Transport Protocol (runtime_checkable)
  - InMemoryTransport: 默认; 同进程直接调 sub.callback(payload)
  - WSTransport skeleton: NotImplementedError 占位
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable


# ==============================================================
# Transport Protocol
# ==============================================================

@runtime_checkable
class Transport(Protocol):
    """Broker → subscriber 的传输层.

    进程内: InMemoryTransport (默认) 直接调 callback.
    远程: WSTransport / SSETransport 序列化通过网络发. (⏳)

    broker 通过 register_subscriber(sub_id, callback) 注册一个 subscription,
    通过 deliver(sub_id, payload) 发送 payload, 通过 unregister(sub_id) 移除.
    """

    def register_subscriber(
        self, sub_id: str, callback: Callable[[dict], None],
    ) -> None: ...

    def unregister(self, sub_id: str) -> None: ...

    def deliver(self, sub_id: str, payload: dict) -> None: ...


# ==============================================================
# InMemoryTransport — 默认实施
# ==============================================================

class InMemoryTransport:
    """默认 transport: 同进程, 直接调 callback.

    callback 抛错时 catch + 继续 (一个坏 subscriber 不污染别人, SPEC §10 AP-R5).
    """

    def __init__(self) -> None:
        self._callbacks: dict[str, Callable[[dict], None]] = {}

    def register_subscriber(
        self, sub_id: str, callback: Callable[[dict], None],
    ) -> None:
        self._callbacks[sub_id] = callback

    def unregister(self, sub_id: str) -> None:
        self._callbacks.pop(sub_id, None)

    def deliver(self, sub_id: str, payload: dict) -> None:
        cb = self._callbacks.get(sub_id)
        if cb is None:
            return
        try:
            cb(payload)
        except Exception:
            # SPEC AP-R5: 一个 subscriber callback 抛错不影响别的
            # 真生产应 logger.exception, 这里静默吞掉 (testing 用 pytest captures)
            pass


# ==============================================================
# WSTransport skeleton — 跨设备同步入口
# ==============================================================

class WSTransport:
    """⏳ WebSocket transport — 跨设备 / 跨 server 同步入口.

    实施待: 业务场景需要跨设备同步时启动 (SPEC §11).
    """

    def __init__(self, ws_url: str | None = None) -> None:
        self._ws_url = ws_url

    def register_subscriber(
        self, sub_id: str, callback: Callable[[dict], None],
    ) -> None:
        raise NotImplementedError("WSTransport 待实施: 需 WebSocket client 连接")

    def unregister(self, sub_id: str) -> None:
        raise NotImplementedError("WSTransport 待实施")

    def deliver(self, sub_id: str, payload: dict) -> None:
        raise NotImplementedError("WSTransport 待实施")


__all__ = [
    "Transport",
    "InMemoryTransport",
    "WSTransport",
]
