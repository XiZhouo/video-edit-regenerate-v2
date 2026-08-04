"""run_status — entity 运行态(会话态 Rendering Hint).

Block1 加载 / 管线 / 占位系统的状态数据型。每个 entity 在管线里处于一个相位:
  pending(占位待跑)/ running(生成中,带 progress)/ done(已产出)/ error(失败可重试)。

红线(editor-architecture):运行态是 Rendering Hint —— 不写进 Entity.data / Document /
OpLog。它由后端 PipelineSupervisor 按文件系统派生、经 SSE entity_status 消息下发,
刷新 / 重连时从磁盘重建。本模块只定数据形态;广播 envelope 在 change_set.py。

待补(跨叶子):
  - progress 节流                 [02-data-flow/01-realtime-sync]
  - 跨设备状态同步                [02-data-flow]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RUN_PHASES = ("pending", "running", "done", "error", "skipped", "interrupted", "stale")


@dataclass(frozen=True)
class StageStatus:
    """多 stage item 内单个 stage 的运行态(P2,加性)。

    phase ∈ RUN_PHASES;progress 0..1 或 None(indeterminate);
    eta_sec / throughput: None = 未知(队列未喂样本时)。

    刻意与 EntityRunStatus 的「默认值省略键」策略不同:StageStatus 是全新型、无历史
    字节基线,故 to_dict() 始终输出固定全键集(含 None)→ 前端拿到稳定 shape,
    不必为「键在不在」做分支。
    """
    stage_id: str
    label: str
    phase: str
    progress: float | None = None
    eta_sec: float | None = None
    throughput: float | None = None

    def __post_init__(self) -> None:
        if self.phase not in RUN_PHASES:
            raise ValueError(f"unknown stage phase {self.phase!r}; expected one of {RUN_PHASES}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "label": self.label,
            "phase": self.phase,
            "progress": self.progress,
            "eta_sec": self.eta_sec,
            "throughput": self.throughput,
        }


@dataclass(frozen=True)
class EntityRunStatus:
    """单 entity 的运行态快照(会话态)。

    progress: 0..1;None = indeterminate(不确定进度)。
    error: {code, message, trace_id} —— phase == 'error' 时携带。
    run_id: 归属的 pipeline run(前端据此 dispatch 重试)。
    placeholder: Supervisor 在 prepare_start 真实新建该实体时签发；不是内容推断。

    多 stage(P2,加性,默认值时 to_dict 整键省略 → 单 stage payload 字节不变):
      stages       : 各 stage 的 StageStatus(空 = 单 stage,省略键)。
      stage_index  : 当前/已完成 stage 序号(None = 单 stage,省略键)。
      eta_sec      : 整个 item 剩余预估秒(None = 未知,省略键)。
      placeholder  : 该 run 启动时由 Supervisor 新建的占位实体。False 省略键。
    """
    entity_type: str
    entity_id: str
    phase: str
    progress: float | None = None
    run_id: str | None = None
    stage_label: str = ""
    error: dict[str, Any] | None = None
    updated_at: float = 0.0
    stages: tuple[StageStatus, ...] = ()
    stage_index: int | None = None
    eta_sec: float | None = None
    placeholder: bool = False

    def __post_init__(self) -> None:
        if self.phase not in RUN_PHASES:
            # 编程错误:supervisor 永远传合法相位
            raise ValueError(f"unknown run phase {self.phase!r}; expected one of {RUN_PHASES}")
        # bool 是 int 的子类；isinstance(1, bool) 不能表达这条 wire 边界。
        if type(self.placeholder) is not bool:
            raise TypeError("placeholder must be an exact bool")

    def to_dict(self) -> dict[str, Any]:
        d = {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "phase": self.phase,
            "progress": self.progress,
            "run_id": self.run_id,
            "stage_label": self.stage_label,
            "error": dict(self.error) if self.error else None,
            "updated_at": self.updated_at,
        }
        # 多 stage 字段:默认值时整键省略 → 单 stage 旧 payload 字节兼容
        if self.stages:
            d["stages"] = [s.to_dict() for s in self.stages]
        if self.stage_index is not None:
            d["stage_index"] = self.stage_index
        if self.eta_sec is not None:
            d["eta_sec"] = self.eta_sec
        # 只有显式占位实体才传输身份；旧 payload 与普通实体保持字节兼容。
        if self.placeholder:
            d["placeholder"] = True
        return d
