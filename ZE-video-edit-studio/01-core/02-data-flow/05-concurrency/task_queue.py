"""QueueManager — 长任务排队 + position + ETA.

完整 SPEC 见同目录 SPEC.md §4.

已实装 (无跨叶子依赖):
  - QueueItem: task_id / stage_id / priority / enqueued_at
  - QueueManager: enqueue / position / estimate_eta_sec / start / finish
                  + waiting / running
  - ETA 基于历史 P90 延迟 (滑动窗口 20 样本; 无样本 fallback 5 秒)
  - priority 高的先出 (数值大=优先); 同 priority FIFO

注意: 本叶子提供数据结构 + 查询 API; 真正的调度 (worker pool / 何时 start)
      是上层工程层的事.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field


# ==============================================================
# QueueItem
# ==============================================================

@dataclass
class QueueItem:
    """排队中的任务."""
    task_id: str
    stage_id: str
    priority: int = 0
    enqueued_at: float = field(default_factory=time.time)


# ==============================================================
# QueueManager
# ==============================================================

_INITIAL_ETA_SEC = 5.0      # 无历史样本时的 ETA 默认值
_SAMPLES_PER_STAGE = 20     # 每 stage 保留多少历史样本


class QueueManager:
    """按 stage 分组的 FIFO + priority queue + ETA 估算."""

    def __init__(self) -> None:
        self._queues: dict[str, list[QueueItem]] = defaultdict(list)
        # task_id → (stage_id, started_at)
        self._running: dict[str, tuple[str, float]] = {}
        # stage_id → deque[float] 历史耗时
        self._duration_samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=_SAMPLES_PER_STAGE)
        )

    # ---- enqueue ----

    def enqueue(self, stage_id: str, *, priority: int = 0) -> QueueItem:
        """入队. 同 priority 内按 enqueued_at 严格 FIFO."""
        item = QueueItem(
            task_id=uuid.uuid4().hex,
            stage_id=stage_id,
            priority=priority,
        )
        q = self._queues[stage_id]
        q.append(item)
        # 高 priority 先; 同 priority 内 stable sort 保 FIFO
        q.sort(key=lambda x: (-x.priority, x.enqueued_at))
        return item

    # ---- 查询 ----

    def _find_in_queue(self, task_id: str) -> tuple[str, int] | None:
        """返回 (stage_id, 0-based index) 或 None."""
        for stage_id, q in self._queues.items():
            for i, item in enumerate(q):
                if item.task_id == task_id:
                    return stage_id, i
        return None

    def position(self, task_id: str) -> int | None:
        """当前队列位置 (1-indexed); 不在队列返 None."""
        found = self._find_in_queue(task_id)
        if found is None:
            return None
        return found[1] + 1

    def estimate_eta_sec(self, task_id: str) -> float:
        """ETA 估算: position × P90 历史耗时. 不在队列返 0."""
        found = self._find_in_queue(task_id)
        if found is None:
            return 0.0
        stage_id, idx = found
        samples = list(self._duration_samples.get(stage_id, []))
        if not samples:
            p90 = _INITIAL_ETA_SEC
        else:
            srt = sorted(samples)
            # P90: 第 90 分位; min 防越界
            p90 = srt[min(len(srt) - 1, int(0.9 * len(srt)))]
        return (idx + 1) * p90

    # ---- 调度入口 ----

    def start(self, task_id: str) -> bool:
        """任务开始跑: 从 queue 摘掉 + 进 _running 表. 返回是否找到."""
        found = self._find_in_queue(task_id)
        if found is None:
            return False
        stage_id, idx = found
        del self._queues[stage_id][idx]
        self._running[task_id] = (stage_id, time.time())
        return True

    def finish(self, task_id: str, *, duration: float | None = None) -> bool:
        """任务完成: 从 _running 表摘掉 + 记录耗时供 ETA 估算.

        duration 不传则从 _running 的 started_at 推算.
        """
        if task_id not in self._running:
            return False
        stage_id, started_at = self._running.pop(task_id)
        actual_duration = duration if duration is not None else (time.time() - started_at)
        if actual_duration > 0:
            self._duration_samples[stage_id].append(actual_duration)
        return True

    # ---- 观察 ----

    def waiting(self, stage_id: str | None = None) -> list[QueueItem]:
        """看队列里在等的任务. stage_id=None 看全部."""
        if stage_id is not None:
            return list(self._queues.get(stage_id, []))
        out: list[QueueItem] = []
        for q in self._queues.values():
            out.extend(q)
        return out

    def running(self) -> list[tuple[str, str]]:
        """在跑的 (task_id, stage_id) 列表."""
        return [(tid, sid) for tid, (sid, _) in self._running.items()]

    def stage_avg_duration(self, stage_id: str) -> float | None:
        """某 stage 的历史 P90 耗时 (用于查诊). 无样本返 None."""
        samples = list(self._duration_samples.get(stage_id, []))
        if not samples:
            return None
        srt = sorted(samples)
        return srt[min(len(srt) - 1, int(0.9 * len(srt)))]
