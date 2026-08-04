"""stage_plan — 一个 WorkItem 经过的有序多 stage 计划.

今天 supervisor 每个 item 只跑一个 item_stage(单 stage);StagePlan 把它推广成
「一个 item 顺序经过 N 个 stage」,每 stage 各自产物、各自资源域、各自进度。

设计约束(加性、向后兼容):
  - 单 stage 旧路径 = StagePlan.from_single(stage)(supervisor P2b 内部桥接);
    长度 1 时广播仍无 stages 键；W05 起 scan 与多 stage 一样只认结构化 done receipt。
  - StagePlan 只描述「顺序与身份」,不持运行态;运行态在 supervisor 内存 + 磁盘 stage 标记。
  - stage 的 run callable 不可序列化 → recover() 续跑需 mod 注册 stage_resolver(P2b);
    本数据型只保证 stage 身份/ports/生命周期声明可 round-trip。

红线:不引入 DAG(非线性依赖)—— items 间仍独立,stage 间仍线性。需要时再扩。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from stage import StageSpec, _thaw_stage_json  # noqa: E402


@dataclass(frozen=True)
class StagePlan:
    """一个 item 顺序经过的 stage 列表(≥1,id 唯一)。

    frozen + tuple → 不可变,可安全跨线程读。
    """
    stages: tuple[StageSpec, ...]
    _stages_snapshot: tuple[StageSpec, ...] = field(
        init=False, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        supplied_stages = object.__getattribute__(self, "stages")
        if type(supplied_stages) is not tuple:
            raise TypeError("StagePlan.stages 必须是精确 tuple[StageSpec, ...]")
        if not supplied_stages:
            raise ValueError("StagePlan 至少需要 1 个 stage")
        if any(type(stage) is not StageSpec for stage in supplied_stages):
            raise TypeError("StagePlan.stages 只能包含精确 StageSpec")

        # The plan owns its execution intent.  Keeping the caller's StageSpec objects would
        # let an old mutable alias change retry/breakpoint/resource semantics after approval.
        # Each clone is rebuilt from StageSpec's hidden construction-time authority snapshot.
        owned_stages = tuple(stage._clone_for_plan() for stage in supplied_stages)
        object.__setattr__(self, "stages", owned_stages)
        object.__setattr__(self, "_stages_snapshot", owned_stages)
        seen: set[str] = set()
        dup: set[str] = set()
        for s in owned_stages:
            # 安全:stage_id 会进 status/<item>.stage.<stage_id>.done 标记文件名。
            if any(c in s.id for c in ("/", "\\", "\x00")):
                raise ValueError(f"StagePlan stage id 不能含路径分隔符 / null: {s.id!r}")
            if s.id in seen:
                dup.add(s.id)
            seen.add(s.id)
        if dup:
            raise ValueError(f"StagePlan stage id 必须唯一,重复: {sorted(dup)}")

    # ---- 构造 ----

    @classmethod
    def from_single(cls, stage: StageSpec) -> "StagePlan":
        """单 stage 桥接:把旧的 item_stage 包成长度 1 的 plan(回退原行为)。"""
        return cls((stage,))

    # ---- 查询 ----

    @property
    def stage_ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.stages)

    @property
    def is_single(self) -> bool:
        return len(self.stages) == 1

    def index_of(self, stage_id: str) -> int:
        """stage_id 的序号(0-based);不存在抛 ValueError。"""
        for i, s in enumerate(self.stages):
            if s.id == stage_id:
                return i
        raise ValueError(f"stage {stage_id!r} 不在 plan 中: {list(self.stage_ids)}")

    def __len__(self) -> int:
        return len(self.stages)

    def __getitem__(self, i: int) -> StageSpec:
        return self.stages[i]

    def to_meta(self) -> list[dict[str, object]]:
        """完整、可序列化的 stage 元数据(manifest / authority 共用;不含 callable)。"""
        result: list[dict[str, object]] = []
        for stage in self.stages:
            (
                stage_id,
                label,
                _run,
                _cancel_handler,
                retry_safe,
                produces,
                resource_scopes,
                resource_costs,
                command_id,
                input_ports,
                output_ports,
                interrupt_safe,
                pause_after,
            ) = stage._authority_values()
            result.append({
                "id": stage_id,
                "label": label,
                "command_id": command_id or stage_id,
                "produces": list(produces),
                "resource_scopes": list(resource_scopes),
                "resource_costs": _thaw_stage_json(resource_costs),
                # These are exact bools validated by StageSpec.  Never normalize a malformed
                # truthy/falsy value here; approval bytes must preserve the declared type.
                "retry_safe": retry_safe,
                "interrupt_safe": interrupt_safe,
                "pause_after": pause_after,
                "input_ports": [_thaw_stage_json(port) for port in input_ports],
                "output_ports": [_thaw_stage_json(port) for port in output_ports],
            })
        return result

    def __getattribute__(self, name: str):
        if name == "stages":
            state = object.__getattribute__(self, "__dict__")
            snapshot = state.get("_stages_snapshot")
            if type(snapshot) is tuple:
                return snapshot
        # Compatibility hardening for Python 3.9 dataclasses: callers receive
        # a detached mapping, never the live frozen-instance storage.
        if name == "__dict__":
            return dict(object.__getattribute__(self, "__dict__"))
        return object.__getattribute__(self, name)

    def __reduce__(self):
        return (type(self), (tuple(self.stages),))
