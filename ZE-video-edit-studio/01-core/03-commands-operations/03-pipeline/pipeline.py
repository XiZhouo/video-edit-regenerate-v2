"""Pipeline — 多 stage 顺序执行 + fan_out + precise retry.

完整 SPEC 见同目录 SPEC.md.

已实装 (无跨叶子依赖):
  - PipelineStep: stage + fan_out
  - PipelineRun: id / steps / status (running/ok/partial/failed/cancelled)
  - PipelineStepResult: step_id / fan_out / results + ok_items / failed_items
  - PipelineRunner: run (顺序 + fan_out 展开 + 失败短路 / continue_on_failure)
                    retry_failed (只重跑失败 item / step)

待补 (跨叶子):
  - 并发 fan_out (thread pool / async)   [caller / 子类自定义]
  - DAG (非线性依赖)                       [按需扩展]
  - 跨 skill stage                         [04-cross-skill-call]
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from stage import StageResult, StageRunner, StageSpec


# ==============================================================
# PipelineStep
# ==============================================================

@dataclass
class PipelineStep:
    """一个步骤: 包一个 StageSpec + 是否 fan_out."""
    stage: StageSpec
    fan_out: bool = False


# ==============================================================
# PipelineStepResult
# ==============================================================

@dataclass
class PipelineStepResult:
    """单 step 结果. fan_out=False 时 results 只 1 个; True 时每 item 1 个."""
    step_id: str
    fan_out: bool
    results: list[StageResult] = field(default_factory=list)

    def ok_items(self) -> list[StageResult]:
        return [r for r in self.results if r.status == "ok"]

    def failed_items(self) -> list[StageResult]:
        """status not in (ok, cancelled) 视为失败."""
        return [r for r in self.results if r.status not in ("ok", "cancelled")]


# ==============================================================
# PipelineRun
# ==============================================================

@dataclass
class PipelineRun:
    """一次 pipeline 跑的状态."""
    id: str
    steps: list[PipelineStepResult] = field(default_factory=list)
    status: str = "running"   # running | ok | partial | failed | cancelled


# ==============================================================
# PipelineRunner
# ==============================================================

class PipelineRunner:
    """顺序跑多 step; fan_out 在 step 内顺序展开多 item."""

    def __init__(self, stage_runner: StageRunner | None = None) -> None:
        self.stage_runner = stage_runner or StageRunner()

    def run(self, steps: list[PipelineStep], args: dict[str, Any]) -> PipelineRun:
        """顺序跑 steps. 失败默认短路; args['continue_on_failure']=True 时继续."""
        run = PipelineRun(id=uuid.uuid4().hex)
        continue_on_failure = bool(args.get("continue_on_failure", False))

        for step in steps:
            step_result = PipelineStepResult(
                step_id=step.stage.id,
                fan_out=step.fan_out,
            )

            if step.fan_out:
                items = args.get("items") or []
                step_args = args.get("step_args") or {}
                for i, item in enumerate(items):
                    r = self.stage_runner.run(
                        step.stage,
                        {"item": item, "_index": i, **step_args},
                    )
                    step_result.results.append(r)
            else:
                r = self.stage_runner.run(step.stage, args)
                step_result.results.append(r)

            run.steps.append(step_result)

            if step_result.failed_items() and not continue_on_failure:
                # 失败短路: 看 pipeline 整体进度有没有 ok
                any_ok = any(s.ok_items() for s in run.steps)
                run.status = "partial" if any_ok else "failed"
                return run

        # 全部步骤跑完
        any_failed = any(s.failed_items() for s in run.steps)
        any_cancelled = any(
            any(r.status == "cancelled" for r in s.results) for s in run.steps
        )
        if any_failed:
            run.status = "partial"
        elif any_cancelled and not any(s.ok_items() for s in run.steps):
            run.status = "cancelled"
        else:
            run.status = "ok"
        return run

    def retry_failed(
        self,
        run: PipelineRun,
        steps: list[PipelineStep],
        args: dict[str, Any],
    ) -> PipelineRun:
        """只重跑 failed_items 的; ok / cancelled 保留.

        retry_safe=False 的 stage 仍会重跑 (本叶子记录字段不强制; caller 决定要不要调).
        """
        step_args = args.get("step_args") or {}
        items = args.get("items") or []

        for i, step in enumerate(steps):
            if i >= len(run.steps):
                break
            step_result = run.steps[i]
            failed = step_result.failed_items()
            if not failed:
                continue

            if step.fan_out:
                new_results: list[StageResult] = []
                for idx, r in enumerate(step_result.results):
                    if r.status not in ("ok", "cancelled"):
                        # 失败的: 用 items[idx] 重跑
                        if idx < len(items):
                            rerun = self.stage_runner.run(
                                step.stage,
                                {"item": items[idx], "_index": idx, **step_args},
                            )
                            new_results.append(rerun)
                        else:
                            # items 不够 (caller 改了 items?) — 保留原结果
                            new_results.append(r)
                    else:
                        new_results.append(r)
                step_result.results = new_results
            else:
                # 非 fan_out: 重跑一次
                rerun = self.stage_runner.run(step.stage, args)
                step_result.results = [rerun]

        # 重算 status
        any_failed = any(s.failed_items() for s in run.steps)
        any_cancelled = any(
            any(r.status == "cancelled" for r in s.results) for s in run.steps
        )
        if any_failed:
            run.status = "partial"
        elif any_cancelled and not any(s.ok_items() for s in run.steps):
            run.status = "cancelled"
        else:
            run.status = "ok"
        return run
