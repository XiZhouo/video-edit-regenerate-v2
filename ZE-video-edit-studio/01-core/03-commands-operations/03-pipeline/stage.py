"""Stage — 长任务单步 (progress + cancel + retry).

完整 SPEC 见同目录 SPEC.md.

已实装 (无跨叶子依赖):
  - StageSpec: id / label / run / cancel_handler / retry_safe
  - StageContext: task_id / stage_id / trace_id / progress / label / eta
                  + set_progress / log / checkpoint / is_cancelled / cancel
  - StageResult: status (ok | cancelled | error) / output / error / 时间戳
  - StageRunner: run (try/except 处理 CancelledError / ZeError / Exception)
                 + cancel(task_id) + active()
  - CancelledError 异常类

待补 (跨叶子):
  - 并发执行 (本叶子同步)               [caller 自包 / 子类覆盖]
  - progress 节流                        [02-data-flow/01-realtime-sync]
  - 中间状态持久化 (pause/resume)        [02-data-flow]
"""

from __future__ import annotations

import math
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 共享 ZeError: 从 02-entity 兄弟目录 import
_ENTITY_DIR = Path(__file__).resolve().parent.parent.parent / "01-data-structure" / "02-entity"
if str(_ENTITY_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTITY_DIR))

from entity import ZeError  # noqa: E402


# ==============================================================
# CancelledError / InterruptedError
# ==============================================================

class CancelledError(Exception):
    """Stage 内部 checkpoint() 命中 cancel 时抛; 由 StageRunner 兜底捕."""


class InterruptedError(Exception):
    """Stage 内部 checkpoint() 命中 interrupt 时抛;可被 supervisor resume。"""


# ==============================================================
# StageContext
# ==============================================================

@dataclass
class StageContext:
    """Stage 运行时上下文. 业务方通过 ctx 报 progress + 检 cancel."""
    task_id: str
    stage_id: str
    trace_id: str
    progress: float = 0.0
    label: str = ""
    eta_sec: float = 0.0
    _cancelled: threading.Event = field(default_factory=threading.Event)
    _interrupted: threading.Event = field(default_factory=threading.Event)
    _progress_cb: Callable[["StageContext"], None] | None = None
    _log_cb: Callable[["StageContext", str, str, dict[str, Any]], None] | None = None

    def set_progress(self, ratio: float, label: str = "", eta_hint: float = 0.0) -> None:
        """更新进度 (0.0~1.0) + 可选 label + 可选 eta; 调 progress callback."""
        self.progress = max(0.0, min(1.0, ratio))
        if label:
            self.label = label
        self.eta_sec = eta_hint
        if self._progress_cb is not None:
            try:
                self._progress_cb(self)
            except Exception:
                # callback 抛错不能影响 stage 执行
                pass

    def log(self, level: str, message: str, **extra: Any) -> None:
        """写 stage 执行日志;无 log callback 时 no-op,callback 抛错不影响 stage。"""
        if self._log_cb is None:
            return
        try:
            self._log_cb(self, level, message, dict(extra))
        except Exception:
            pass

    def checkpoint(self) -> None:
        """检 cancel/interrupt; 业务方在 stage.run 内周期性调."""
        if self._cancelled.is_set():
            raise CancelledError(f"stage {self.stage_id!r} cancelled (task={self.task_id})")
        if self._interrupted.is_set():
            raise InterruptedError(f"stage {self.stage_id!r} interrupted (task={self.task_id})")

    def interrupt_checkpoint(self) -> None:
        """Jupyter 式 interrupt 检查点;语义同 checkpoint(),保留给 stage 显式调用。"""
        self.checkpoint()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def is_interrupted(self) -> bool:
        return self._interrupted.is_set()

    def cancel(self) -> None:
        """外部触发取消. 标记 cancel; 下次 checkpoint() 时生效."""
        self._cancelled.set()

    def interrupt(self) -> None:
        """外部触发中断. 标记 interrupt; 下次 checkpoint() 时生效。"""
        self._interrupted.set()


# ==============================================================
# StageResult
# ==============================================================

@dataclass
class StageResult:
    """Stage 完成结果. status ∈ {ok, cancelled, interrupted, error}."""
    task_id: str
    stage_id: str
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    started_at: float = 0.0
    ended_at: float = 0.0


# ==============================================================
# StageSpec
# ==============================================================

class _FrozenJsonMap(Mapping[str, Any]):
    """Deeply immutable mapping used by StageSpec's approved execution intent.

    This deliberately is *not* a ``dict`` subclass.  Overriding ``dict`` mutators is
    insufficient for an authority object because ``dict.__setitem__(value, ...)`` can
    bypass those overrides.  A tuple-backed Mapping keeps the familiar read API while
    leaving no mutable base container for that bypass to target.
    """

    __slots__ = ("_entries",)
    __hash__ = None

    def __init__(self, entries: tuple[tuple[str, Any], ...]) -> None:
        object.__setattr__(self, "_entries", entries)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("StageSpec JSON snapshot is immutable")

    def __delattr__(self, _name: str) -> None:
        raise TypeError("StageSpec JSON snapshot is immutable")

    def __getitem__(self, key: str) -> Any:
        for candidate, value in self._entries:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return repr(_thaw_stage_json(self))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Mapping) or len(self) != len(other):
            return False
        try:
            return all(key in other and value == other[key] for key, value in self._entries)
        except (KeyError, TypeError, ValueError):
            return False

    def copy(self) -> dict[str, Any]:
        """Return a detached mutable dict, matching the useful part of dict.copy()."""
        return _thaw_stage_json(self)

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        return (_freeze_stage_json, (_thaw_stage_json(self),))


class _FrozenJsonList(Sequence[Any]):
    """List-shaped immutable JSON view for nested port extension values."""

    __slots__ = ("_values",)

    def __init__(self, values: tuple[Any, ...]) -> None:
        object.__setattr__(self, "_values", values)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("StageSpec JSON snapshot is immutable")

    def __delattr__(self, _name: str) -> None:
        raise TypeError("StageSpec JSON snapshot is immutable")

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [_thaw_stage_json(value) for value in self._values[index]]
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return repr(_thaw_stage_json(self))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, (list, tuple, _FrozenJsonList)):
            return False
        return len(self) == len(other) and all(
            left == right for left, right in zip(self, other)
        )

    def copy(self) -> list[Any]:
        return _thaw_stage_json(self)

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        return (_freeze_stage_json, (_thaw_stage_json(self),))


def _freeze_stage_json(
    value: Any,
    path: str = "stage JSON",
    active: set[int] | None = None,
) -> Any:
    """Own one exact JSON graph without retaining a mutable caller container."""
    active = set() if active is None else active
    value_type = type(value)
    if value is None or value_type in (bool, int, str):
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} 不能包含 NaN 或 Infinity")
        return value
    if value_type is dict:
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} 不能包含容器循环")
        active.add(identity)
        try:
            entries: list[tuple[str, Any]] = []
            for key, member in value.items():
                if type(key) is not str:
                    raise TypeError(f"{path} 的 object key 必须是精确 str")
                entries.append((key, _freeze_stage_json(member, f"{path}.{key}", active)))
            return _FrozenJsonMap(tuple(entries))
        finally:
            active.remove(identity)
    if value_type is list:
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} 不能包含容器循环")
        active.add(identity)
        try:
            return _FrozenJsonList(tuple(
                _freeze_stage_json(member, f"{path}[{index}]", active)
                for index, member in enumerate(value)
            ))
        finally:
            active.remove(identity)
    raise TypeError(f"{path} 只能包含精确 JSON 类型")


def _thaw_stage_json(value: Any) -> Any:
    """Return a detached plain JSON carrier from a frozen StageSpec member."""
    if isinstance(value, _FrozenJsonMap):
        # Iterate the tuple-backed storage directly.  Mapping.items() would perform one
        # linear __getitem__ lookup per key and turn a large approved extension bag into O(n²).
        entries = object.__getattribute__(value, "_entries")
        return {key: _thaw_stage_json(member) for key, member in entries}
    if isinstance(value, _FrozenJsonList):
        return [_thaw_stage_json(member) for member in value]
    return value


def _exact_string_tuple(name: str, value: Any) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(member) is not str for member in value):
        raise TypeError(f"StageSpec.{name} 必须是精确 tuple[str, ...]")
    if any(not member for member in value):
        raise ValueError(f"StageSpec.{name} 不能包含空字符串")
    if len(set(value)) != len(value):
        raise ValueError(f"StageSpec.{name} 不能包含重复值")
    return tuple(value)


_STAGE_AUTHORITY_FIELD_INDEX = {
    "id": 0,
    "label": 1,
    "run": 2,
    "cancel_handler": 3,
    "retry_safe": 4,
    "produces": 5,
    "resource_scopes": 6,
    "resource_costs": 7,
    "command_id": 8,
    "input_ports": 9,
    "output_ports": 10,
    "interrupt_safe": 11,
    "pause_after": 12,
}


@dataclass(frozen=True)
class StageSpec:
    """Stage 声明.

    run 函数签名: (ctx: StageContext, args: dict) -> dict
      - 内部周期性调 ctx.checkpoint() 让 cancel 能生效
      - 通过 ctx.set_progress() 报告进度
      - 副作用 (改 ZE 数据) 必须走 command dispatch
      - 返回 dict (落 StageResult.output)
      - 失败抛 ZeError 或其他 Exception

    多 stage(P2,加性,均默认空 → 单 stage 旧行为不变):
      produces        : 本 stage 期望落盘的产物名(相对路径片段);供 StagePlan/调度
                        描述「哪一步产出什么」,不强制 —— 真正 done 判定走 stage 完成标记。
      resource_scopes : 本 stage 进入前需获取的资源域(GateRegistry 据此 acquire);
                        默认空 = 不限流(全兼容)。
      resource_costs  : 容量类资源域(CapacityGate,如显存 MB)每次准入的加权 cost;
                        {scope: cost}。缺省该 scope → cost=1。槽位类(ResourceGate)忽略本字段。
      command_id      : 用户可见 command cell id;空时回退 stage.id。
      input_ports     : stage 开始前快照/展示的输入端口。
      output_ports    : stage 完成后快照/展示的输出端口。
      interrupt_safe  : 是否允许 interrupt 后从该 stage 边界继续。
      retry_safe      : 该 stage 是否可被重放。Supervisor 在自动重试、
                        retry_failed、rerun/run-from 以及崩溃恢复续跑前
                        强制检查实际执行的 suffix。False 表示发送消息/
                        邮件等非幂等副作用，重放会以 pipeline_replay_unsafe
                        拒绝。默认 True 是对现有纯本地 stage 的兼容语义；
                        新的外部副作用 stage 必须显式填 False。
    """
    id: str
    label: str
    run: Callable[[StageContext, dict[str, Any]], dict[str, Any]]
    cancel_handler: Callable[[StageContext], None] | None = None
    retry_safe: bool = True
    produces: tuple[str, ...] = ()
    resource_scopes: tuple[str, ...] = ()
    resource_costs: dict[str, int] = field(default_factory=dict)
    command_id: str = ""
    input_ports: tuple[dict[str, Any], ...] = ()
    output_ports: tuple[dict[str, Any], ...] = ()
    interrupt_safe: bool = False
    # 「关闭」断点:本 stage 完成后自动停在边界(run 转 paused,item 留 pending、可 resume 续跑)。
    # 静态声明(mod 默认)/ 运行时经 set_breakpoint 动态切。默认 False = 无断点、与现状字节等价。
    pause_after: bool = False
    _authority_snapshot: tuple[Any, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.id) is not str
            or not self.id
            or self.id in (".", "..")
            or any(c in self.id for c in ("/", "\\", "\x00"))
        ):
            raise ValueError(f"StageSpec.id 必须是安全的非空标识: {self.id!r}")
        if type(self.label) is not str:
            raise TypeError("StageSpec.label 必须是精确 str")
        if not callable(self.run):
            raise TypeError("StageSpec.run 必须可调用")
        if self.cancel_handler is not None and not callable(self.cancel_handler):
            raise TypeError("StageSpec.cancel_handler 必须为 None 或 callable")
        for name in ("retry_safe", "interrupt_safe", "pause_after"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"StageSpec.{name} 必须是精确 bool")
        if type(self.command_id) is not str:
            raise TypeError("StageSpec.command_id 必须是精确 str")

        produces = _exact_string_tuple("produces", self.produces)
        resource_scopes = _exact_string_tuple("resource_scopes", self.resource_scopes)
        if type(self.resource_costs) is not dict:
            raise TypeError("StageSpec.resource_costs 必须是精确 dict[str, int]")
        for scope, cost in self.resource_costs.items():
            if type(scope) is not str or not scope:
                raise TypeError("StageSpec.resource_costs key 必须是非空精确 str")
            if type(cost) is not int or cost < 1:
                raise TypeError("StageSpec.resource_costs value 必须是正的精确 int")
            if scope not in resource_scopes:
                raise ValueError("StageSpec.resource_costs 只能声明 resource_scopes 中的资源")
        resource_costs = _freeze_stage_json(self.resource_costs, "StageSpec.resource_costs")

        owned_ports: list[tuple[_FrozenJsonMap, ...]] = []
        for name in ("input_ports", "output_ports"):
            ports = getattr(self, name)
            if type(ports) is not tuple:
                raise TypeError(f"StageSpec.{name} 必须是精确 tuple[dict, ...]")
            frozen_ports: list[_FrozenJsonMap] = []
            port_ids: list[str] = []
            for index, port in enumerate(ports):
                if type(port) is not dict:
                    raise TypeError(f"StageSpec.{name}[{index}] 必须是精确 dict")
                port_id = port.get("id")
                if type(port_id) is not str or not port_id:
                    raise ValueError(f"StageSpec.{name}[{index}].id 必须是非空精确 str")
                frozen = _freeze_stage_json(port, f"StageSpec.{name}[{index}]")
                if not isinstance(frozen, _FrozenJsonMap):
                    raise TypeError(f"StageSpec.{name}[{index}] 必须是 JSON object")
                frozen_ports.append(frozen)
                port_ids.append(port_id)
            if len(set(port_ids)) != len(port_ids):
                raise ValueError(f"StageSpec.{name} 的 port id 必须唯一")
            owned_ports.append(tuple(frozen_ports))

        input_ports, output_ports = owned_ports
        object.__setattr__(self, "produces", produces)
        object.__setattr__(self, "resource_scopes", resource_scopes)
        object.__setattr__(self, "resource_costs", resource_costs)
        object.__setattr__(self, "input_ports", input_ports)
        object.__setattr__(self, "output_ports", output_ports)
        object.__setattr__(self, "_authority_snapshot", (
            self.id,
            self.label,
            self.run,
            self.cancel_handler,
            self.retry_safe,
            produces,
            resource_scopes,
            resource_costs,
            self.command_id,
            input_ports,
            output_ports,
            self.interrupt_safe,
            self.pause_after,
        ))

    def _clone_for_plan(self) -> "StageSpec":
        """Build a detached execution snapshot from construction-time authority values."""
        (
            stage_id,
            label,
            run,
            cancel_handler,
            retry_safe,
            produces,
            resource_scopes,
            resource_costs,
            command_id,
            input_ports,
            output_ports,
            interrupt_safe,
            pause_after,
        ) = self._authority_snapshot
        return StageSpec(
            id=stage_id,
            label=label,
            run=run,
            cancel_handler=cancel_handler,
            retry_safe=retry_safe,
            produces=tuple(produces),
            resource_scopes=tuple(resource_scopes),
            resource_costs=_thaw_stage_json(resource_costs),
            command_id=command_id,
            input_ports=tuple(_thaw_stage_json(port) for port in input_ports),
            output_ports=tuple(_thaw_stage_json(port) for port in output_ports),
            interrupt_safe=interrupt_safe,
            pause_after=pause_after,
        )

    def _authority_values(self) -> tuple[Any, ...]:
        """Private read-only values used by StagePlan's sole public serializer."""
        return self._authority_snapshot

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        """Rebuild through validation; the detached ``__dict__`` view is not pickle state."""
        (
            stage_id,
            label,
            run,
            cancel_handler,
            retry_safe,
            produces,
            resource_scopes,
            resource_costs,
            command_id,
            input_ports,
            output_ports,
            interrupt_safe,
            pause_after,
        ) = self._authority_snapshot
        return (
            type(self),
            (
                stage_id,
                label,
                run,
                cancel_handler,
                retry_safe,
                tuple(produces),
                tuple(resource_scopes),
                _thaw_stage_json(resource_costs),
                command_id,
                tuple(_thaw_stage_json(port) for port in input_ports),
                tuple(_thaw_stage_json(port) for port in output_ports),
                interrupt_safe,
                pause_after,
            ),
        )

    def __getattribute__(self, name: str) -> Any:
        # All execution-facing reads come from the same construction snapshot
        # that StagePlan serializes.  Even a deliberate write to a public
        # dataclass field cannot make runtime execution diverge from approval.
        authority_index = _STAGE_AUTHORITY_FIELD_INDEX.get(name)
        if authority_index is not None:
            state = object.__getattribute__(self, "__dict__")
            snapshot = state.get("_authority_snapshot")
            if type(snapshot) is tuple and len(snapshot) == 13:
                member = snapshot[authority_index]
                # Preserve the legacy public dict/tuple-of-dicts read shape without exposing
                # the tuple-backed authority containers.  Every read is a detached projection;
                # execution and serialization continue to consume the private snapshot.
                if name == "resource_costs":
                    return _thaw_stage_json(member)
                if name in ("input_ports", "output_ports"):
                    return tuple(_thaw_stage_json(port) for port in member)
                return member
        # Python 3.9's dataclass has no slots= support.  Never expose the live
        # storage mapping: mutating ``spec.__dict__`` must not bypass frozen.
        if name == "__dict__":
            return dict(object.__getattribute__(self, "__dict__"))
        return object.__getattribute__(self, name)


# ==============================================================
# StageRunner
# ==============================================================

class StageRunner:
    """同步跑单 stage; try/except 统一处理 cancel / error.

    用法:
        runner = StageRunner()
        result = runner.run(spec, args, on_progress=cb)
        # 另一线程 / async context:
        runner.cancel(task_id)
    """

    def __init__(self) -> None:
        self._tasks: dict[str, StageContext] = {}

    def run(
        self,
        spec: StageSpec,
        args: dict[str, Any],
        *,
        task_id: str | None = None,
        on_progress: Callable[[StageContext], None] | None = None,
        on_log: Callable[[StageContext, str, str, dict[str, Any]], None] | None = None,
        trace_id: str | None = None,
    ) -> StageResult:
        """同步跑 stage. 阻塞直到完成 / cancel / error.

        caller 想异步用就自己包 thread / asyncio.to_thread.
        """
        ctx = StageContext(
            task_id=task_id or uuid.uuid4().hex,
            stage_id=spec.id,
            trace_id=trace_id or uuid.uuid4().hex,
            _progress_cb=on_progress,
            _log_cb=on_log,
        )
        self._tasks[ctx.task_id] = ctx
        result = StageResult(
            task_id=ctx.task_id,
            stage_id=spec.id,
            status="ok",
            started_at=time.time(),
        )
        try:
            output = spec.run(ctx, args)
            if not isinstance(output, dict):
                # 业务方写错; 简化为空 dict 不抛
                output = {}
            result.output = output
            result.status = "ok"
            ctx.set_progress(1.0, label="done")
        except CancelledError as e:
            result.status = "cancelled"
            result.error = {"code": "cancelled", "message": str(e),
                            "trace_id": ctx.trace_id}
            if spec.cancel_handler is not None:
                try:
                    spec.cancel_handler(ctx)
                except Exception:
                    pass
        except InterruptedError as e:
            result.status = "interrupted"
            result.error = {"code": "interrupted", "message": str(e),
                            "trace_id": ctx.trace_id}
        except ZeError as e:
            result.status = "error"
            result.error = {
                "code": e.code,
                "message": str(e.args[0]) if e.args else "",
                "context": dict(e.context),
                "trace_id": ctx.trace_id,
            }
        except Exception as e:
            result.status = "error"
            result.error = {
                "code": "stage_error",
                "message": f"{type(e).__name__}: {e}",
                "trace_id": ctx.trace_id,
            }
        finally:
            result.ended_at = time.time()
            self._tasks.pop(ctx.task_id, None)
        return result

    def cancel(self, task_id: str) -> bool:
        """标记某 task 取消. 返回是否找到. 实际中断在 stage 下次 checkpoint()."""
        ctx = self._tasks.get(task_id)
        if ctx is None:
            return False
        ctx.cancel()
        return True

    def interrupt(self, task_id: str) -> bool:
        """标记某 task 中断. 返回是否找到. 实际中断在 stage 下次 checkpoint()."""
        ctx = self._tasks.get(task_id)
        if ctx is None:
            return False
        ctx.interrupt()
        return True

    def active(self) -> list[str]:
        """在跑的 task_id 列表."""
        return list(self._tasks.keys())
