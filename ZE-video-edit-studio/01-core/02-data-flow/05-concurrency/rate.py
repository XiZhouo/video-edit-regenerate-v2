"""RateLimiter — token bucket 限流.

完整 SPEC 见同目录 SPEC.md §5.

已实装 (无跨叶子依赖):
  - RateLimiter: configure / acquire / wait_for / available
  - token bucket 算法 (capacity + rate)
  - 未配置 key 默认放行 (避免"忘配置导致全拒"的坑)
  - wait_for 阻塞等 (内部 sleep poll; async 场景 caller 自包)

线程安全: 内部 _buckets 操作由 threading.Lock 保护 (06-resource-budget 的
ResourceGate 会并发调用同一 key 的 wait_for/acquire).
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# 共享 ZeError: 从 02-entity 兄弟目录 import
_ENTITY_DIR = Path(__file__).resolve().parent.parent.parent / "01-data-structure" / "02-entity"
if str(_ENTITY_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTITY_DIR))

from entity import ZeError  # noqa: E402


_POLL_INTERVAL_SEC = 0.05    # wait_for 的轮询间隔


@dataclass
class _Bucket:
    """单个 key 的 token bucket 状态."""
    capacity: float
    rate: float
    tokens: float
    last_refill: float


class RateLimiter:
    """token bucket 限流器. 多 key 独立配置.

    用法:
        rl = RateLimiter()
        rl.configure("openai-api", rate=5.0, capacity=10.0)
        if rl.acquire("openai-api"):
            do_api_call()
        # 或阻塞等
        if rl.wait_for("openai-api", timeout=30.0):
            do_api_call()
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()    # 保护 _buckets 读写; 让多线程 (如 ResourceGate) 安全共用

    # ---- 配置 ----

    def configure(self, key: str, *, rate: float, capacity: float) -> None:
        """配某 key 的限流参数.

        rate: 每秒补充 token 数
        capacity: 桶容量 (max token 数, 允许的最大 burst)
        """
        if rate <= 0:
            raise ZeError(
                "validation_failed",
                f"rate must be positive, got {rate}",
                context={"key": key, "rate": rate},
            )
        if capacity <= 0:
            raise ZeError(
                "validation_failed",
                f"capacity must be positive, got {capacity}",
                context={"key": key, "capacity": capacity},
            )
        # 新建或重配 (重配也重置 tokens 到 capacity 满桶起步)
        with self._lock:
            self._buckets[key] = _Bucket(
                capacity=capacity,
                rate=rate,
                tokens=capacity,
                last_refill=time.time(),
            )

    def is_configured(self, key: str) -> bool:
        with self._lock:
            return key in self._buckets

    # ---- 内部: refill ----

    def _refill(self, bucket: _Bucket) -> None:
        now = time.time()
        elapsed = now - bucket.last_refill
        if elapsed <= 0:
            return
        bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.rate)
        bucket.last_refill = now

    # ---- acquire / wait_for ----

    def acquire(self, key: str, n: float = 1.0) -> bool:
        """尝试拿 n 个 token. 够则扣除返 True; 不够返 False (不等待).

        未配置 key 默认放行 (返 True).
        """
        if n <= 0:
            return True
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return True             # 未配置默认放行
            self._refill(bucket)
            if bucket.tokens >= n:
                bucket.tokens -= n
                return True
            return False

    def wait_for(self, key: str, n: float = 1.0, *, timeout: float | None = None) -> bool:
        """阻塞等到能拿到 n 个 token. 超时返 False; 拿到返 True.

        未配置 key 立即放行.
        timeout=None 表示无限等 (有死等风险, caller 责任).
        """
        if n <= 0:
            return True
        with self._lock:
            if self._buckets.get(key) is None:
                return True         # 未配置立即放行
        # 不在持锁状态下 sleep; acquire() 自带锁, 轮询期间别的线程可正常 refill/acquire
        deadline = (time.time() + timeout) if timeout is not None else None
        while True:
            if self.acquire(key, n):
                return True
            if deadline is not None and time.time() >= deadline:
                return False
            time.sleep(_POLL_INTERVAL_SEC)

    def available(self, key: str) -> float:
        """当前 token 数 (查诊用; 未配置返 inf 表示无限制)."""
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return float("inf")
            self._refill(bucket)
            return bucket.tokens
