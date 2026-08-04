"""test_pipeline.py — Stage + Pipeline 行为测试.

覆盖:
  - StageSpec / StageContext / StageResult
  - StageRunner: 跑通 / 抛 ZeError / 抛 Exception / cancel / progress callback
  - PipelineRunner: 顺序 / fan_out / 失败短路 / continue_on_failure / retry_failed
  - cancel 协议: checkpoint() 命中抛 CancelledError → status=cancelled
"""

from __future__ import annotations

import threading
import time

import pytest

from stage import (
    CancelledError,
    StageContext,
    StageResult,
    StageRunner,
    StageSpec,
)
from pipeline import (
    PipelineRun,
    PipelineRunner,
    PipelineStep,
    PipelineStepResult,
)
from entity import ZeError


# ==============================================================
# Stage 基础
# ==============================================================

def test_stage_run_returns_ok():
    runner = StageRunner()
    spec = StageSpec(
        id="hello",
        label="hello stage",
        run=lambda ctx, args: {"greeting": f"hi {args['name']}"},
    )
    result = runner.run(spec, {"name": "world"})
    assert result.status == "ok"
    assert result.output == {"greeting": "hi world"}
    assert result.stage_id == "hello"
    assert result.started_at > 0
    assert result.ended_at >= result.started_at


def test_stage_run_assigns_task_id():
    runner = StageRunner()
    spec = StageSpec(id="x", label="x", run=lambda ctx, args: {})
    result = runner.run(spec, {})
    assert len(result.task_id) == 32  # uuid4 hex


def test_stage_run_uses_explicit_task_id():
    runner = StageRunner()
    spec = StageSpec(id="x", label="x", run=lambda ctx, args: {})
    result = runner.run(spec, {}, task_id="custom-task")
    assert result.task_id == "custom-task"


def test_stage_run_non_dict_output_falls_back_empty():
    """业务方误返回非 dict, fallback 空 dict 不崩."""
    runner = StageRunner()
    spec = StageSpec(id="x", label="x", run=lambda ctx, args: "not a dict")
    result = runner.run(spec, {})
    assert result.status == "ok"
    assert result.output == {}


# ==============================================================
# Stage progress
# ==============================================================

def test_stage_progress_callback_fires():
    seen = []
    def cb(ctx):
        seen.append((ctx.progress, ctx.label))

    runner = StageRunner()
    spec = StageSpec(
        id="prog", label="p",
        run=lambda ctx, args: (ctx.set_progress(0.5, "half"), {"done": True})[1],
    )
    result = runner.run(spec, {}, on_progress=cb)
    assert result.status == "ok"
    # 应该有 progress=0.5 和 final progress=1.0
    progresses = [p for p, _ in seen]
    assert 0.5 in progresses
    assert 1.0 in progresses


def test_stage_progress_clamps_to_0_1():
    """progress 自动 clamp 到 [0, 1]."""
    runner = StageRunner()
    final_ctx = []
    def cb(ctx):
        final_ctx.append(ctx.progress)

    def run(ctx, args):
        ctx.set_progress(-0.5)
        ctx.set_progress(2.0)
        return {}

    spec = StageSpec(id="x", label="x", run=run)
    runner.run(spec, {}, on_progress=cb)
    assert min(final_ctx) >= 0.0
    assert max(final_ctx) <= 1.0


def test_progress_callback_exception_swallowed():
    """callback 抛错不应影响 stage 执行."""
    def cb(ctx):
        raise RuntimeError("bad callback")

    runner = StageRunner()
    spec = StageSpec(
        id="x", label="x",
        run=lambda ctx, args: (ctx.set_progress(0.5), {"ok": True})[1],
    )
    result = runner.run(spec, {}, on_progress=cb)
    assert result.status == "ok"


# ==============================================================
# Stage error handling
# ==============================================================

def test_stage_ze_error_caught():
    runner = StageRunner()
    def run(ctx, args):
        raise ZeError("validation_failed", "bad input", context={"k": "v"})

    spec = StageSpec(id="x", label="x", run=run)
    result = runner.run(spec, {})
    assert result.status == "error"
    assert result.error["code"] == "validation_failed"
    assert "bad input" in result.error["message"]


def test_stage_generic_exception_caught():
    runner = StageRunner()
    def run(ctx, args):
        raise RuntimeError("boom")

    spec = StageSpec(id="x", label="x", run=run)
    result = runner.run(spec, {})
    assert result.status == "error"
    assert result.error["code"] == "stage_error"
    assert "RuntimeError" in result.error["message"]


# ==============================================================
# Stage cancel
# ==============================================================

def test_stage_cancel_via_checkpoint():
    """另一线程 cancel; stage 内 checkpoint 抛 CancelledError."""
    runner = StageRunner()

    def run(ctx, args):
        for i in range(100):
            ctx.checkpoint()
            time.sleep(0.005)
        return {}

    spec = StageSpec(id="long", label="long", run=run)
    task_id = "cancel-test"

    # 启动 stage in thread
    result_holder = []
    def go():
        result_holder.append(runner.run(spec, {}, task_id=task_id))
    t = threading.Thread(target=go)
    t.start()
    time.sleep(0.05)              # 让 stage 跑一会
    canceled = runner.cancel(task_id)
    t.join()

    assert canceled is True
    assert result_holder[0].status == "cancelled"
    assert result_holder[0].error["code"] == "cancelled"


def test_stage_cancel_unknown_task_returns_false():
    runner = StageRunner()
    assert runner.cancel("nope") is False


def test_stage_cancel_handler_called():
    """cancel 触发后, spec.cancel_handler 应被调用."""
    handler_called = []
    def cancel_handler(ctx):
        handler_called.append(ctx.task_id)

    runner = StageRunner()
    def run(ctx, args):
        for _ in range(100):
            ctx.checkpoint()
            time.sleep(0.005)
        return {}

    spec = StageSpec(id="x", label="x", run=run, cancel_handler=cancel_handler)

    result_holder = []
    def go():
        result_holder.append(runner.run(spec, {}, task_id="t1"))
    t = threading.Thread(target=go)
    t.start()
    time.sleep(0.05)
    runner.cancel("t1")
    t.join()

    assert result_holder[0].status == "cancelled"
    assert handler_called == ["t1"]


def test_stage_cancel_handler_exception_swallowed():
    """cancel_handler 抛错不影响 status."""
    def bad_handler(ctx):
        raise RuntimeError("handler broke")

    runner = StageRunner()
    def run(ctx, args):
        for _ in range(100):
            ctx.checkpoint()
            time.sleep(0.005)
        return {}

    spec = StageSpec(id="x", label="x", run=run, cancel_handler=bad_handler)
    result_holder = []
    def go():
        result_holder.append(runner.run(spec, {}, task_id="t2"))
    t = threading.Thread(target=go)
    t.start()
    time.sleep(0.05)
    runner.cancel("t2")
    t.join()
    assert result_holder[0].status == "cancelled"


def test_stage_active_lists_running_tasks():
    """active() 在 stage 跑时含 task_id; 完成后清."""
    runner = StageRunner()
    seen_active = []

    def run(ctx, args):
        seen_active.append(list(runner.active()))
        return {}

    spec = StageSpec(id="x", label="x", run=run)
    runner.run(spec, {}, task_id="active-test")
    assert seen_active[0] == ["active-test"]
    # 完成后清
    assert runner.active() == []


# ==============================================================
# StageContext checkpoint without cancel
# ==============================================================

def test_checkpoint_no_cancel_is_noop():
    """无 cancel 时 checkpoint 应该 noop 不抛."""
    runner = StageRunner()
    def run(ctx, args):
        for _ in range(10):
            ctx.checkpoint()
        return {"loops": 10}

    spec = StageSpec(id="x", label="x", run=run)
    result = runner.run(spec, {})
    assert result.status == "ok"
    assert result.output == {"loops": 10}


# ==============================================================
# Pipeline 基础 (non-fan_out)
# ==============================================================

def test_pipeline_sequential_steps():
    runner = PipelineRunner()
    stage_a = StageSpec(id="a", label="a", run=lambda ctx, args: {"from_a": 1})
    stage_b = StageSpec(id="b", label="b", run=lambda ctx, args: {"from_b": 2})

    run = runner.run([PipelineStep(stage_a), PipelineStep(stage_b)], {})

    assert run.status == "ok"
    assert len(run.steps) == 2
    assert run.steps[0].step_id == "a"
    assert run.steps[1].step_id == "b"
    assert run.steps[0].results[0].output == {"from_a": 1}


def test_pipeline_run_has_unique_id():
    runner = PipelineRunner()
    stage = StageSpec(id="x", label="x", run=lambda ctx, args: {})
    run1 = runner.run([PipelineStep(stage)], {})
    run2 = runner.run([PipelineStep(stage)], {})
    assert run1.id != run2.id


def test_pipeline_empty_steps():
    runner = PipelineRunner()
    run = runner.run([], {})
    assert run.status == "ok"
    assert run.steps == []


# ==============================================================
# Pipeline fan_out
# ==============================================================

def test_pipeline_fan_out_runs_per_item():
    runner = PipelineRunner()
    stage = StageSpec(
        id="proc", label="p",
        run=lambda ctx, args: {"processed": args["item"], "idx": args["_index"]},
    )
    run = runner.run(
        [PipelineStep(stage, fan_out=True)],
        {"items": ["x", "y", "z"]},
    )
    assert run.status == "ok"
    assert len(run.steps) == 1
    assert run.steps[0].fan_out is True
    assert len(run.steps[0].results) == 3
    assert run.steps[0].results[0].output == {"processed": "x", "idx": 0}
    assert run.steps[0].results[2].output == {"processed": "z", "idx": 2}


def test_pipeline_fan_out_empty_items():
    runner = PipelineRunner()
    stage = StageSpec(id="x", label="x", run=lambda ctx, args: {})
    run = runner.run(
        [PipelineStep(stage, fan_out=True)],
        {"items": []},
    )
    assert run.status == "ok"
    assert len(run.steps[0].results) == 0


def test_pipeline_fan_out_step_args_passed():
    """fan_out 时 args['step_args'] 应合并进每 item 的 args."""
    seen_args = []
    stage = StageSpec(
        id="x", label="x",
        run=lambda ctx, args: (seen_args.append(dict(args)), {})[1],
    )
    runner = PipelineRunner()
    runner.run(
        [PipelineStep(stage, fan_out=True)],
        {"items": ["a", "b"], "step_args": {"shared_key": "shared_val"}},
    )
    for a in seen_args:
        assert a["shared_key"] == "shared_val"


# ==============================================================
# Pipeline 失败处理
# ==============================================================

def test_pipeline_failure_short_circuits():
    """默认: 一旦 step 失败, 后续 step 不跑."""
    runner = PipelineRunner()
    ran = []
    def good(ctx, args):
        ran.append("a")
        return {}
    def bad(ctx, args):
        ran.append("b")
        raise ZeError("validation_failed", "bad")
    def never(ctx, args):
        ran.append("c")
        return {}

    run = runner.run([
        PipelineStep(StageSpec(id="a", label="a", run=good)),
        PipelineStep(StageSpec(id="b", label="b", run=bad)),
        PipelineStep(StageSpec(id="c", label="c", run=never)),
    ], {})

    assert ran == ["a", "b"]   # c 没跑
    assert run.status == "partial"  # 有 ok (a) + 有 failed (b)


def test_pipeline_continue_on_failure():
    runner = PipelineRunner()
    ran = []
    def good(ctx, args):
        ran.append("a")
        return {}
    def bad(ctx, args):
        ran.append("b")
        raise ZeError("validation_failed", "bad")
    def after_bad(ctx, args):
        ran.append("c")
        return {}

    run = runner.run([
        PipelineStep(StageSpec(id="a", label="a", run=good)),
        PipelineStep(StageSpec(id="b", label="b", run=bad)),
        PipelineStep(StageSpec(id="c", label="c", run=after_bad)),
    ], {"continue_on_failure": True})

    assert ran == ["a", "b", "c"]
    assert run.status == "partial"


def test_pipeline_all_failed_status():
    runner = PipelineRunner()
    def bad(ctx, args):
        raise ZeError("validation_failed", "bad")

    run = runner.run(
        [PipelineStep(StageSpec(id="a", label="a", run=bad))],
        {},
    )
    assert run.status == "failed"


def test_pipeline_fan_out_mixed_results():
    """fan_out 部分 item 失败, status=partial."""
    runner = PipelineRunner()
    def run_item(ctx, args):
        if args["item"] == "bad":
            raise ZeError("validation_failed", "bad item")
        return {"item": args["item"]}

    stage = StageSpec(id="proc", label="p", run=run_item)
    run = runner.run(
        [PipelineStep(stage, fan_out=True)],
        {"items": ["a", "bad", "c"]},
    )
    assert run.status == "partial"
    step = run.steps[0]
    assert len(step.ok_items()) == 2
    assert len(step.failed_items()) == 1


# ==============================================================
# retry_failed
# ==============================================================

def test_retry_failed_only_reruns_failed():
    runner = PipelineRunner()
    counts: dict[str, int] = {}
    def run_item(ctx, args):
        item = args["item"]
        counts[item] = counts.get(item, 0) + 1
        if item == "x" and counts[item] == 1:
            raise ZeError("validation_failed", "first try fails")
        return {"item": item, "try": counts[item]}

    stage = StageSpec(id="proc", label="p", run=run_item)
    args = {"items": ["a", "x", "c"]}
    run = runner.run([PipelineStep(stage, fan_out=True)], args)

    assert run.status == "partial"
    assert counts == {"a": 1, "x": 1, "c": 1}

    # retry
    run = runner.retry_failed(run, [PipelineStep(stage, fan_out=True)], args)
    assert run.status == "ok"
    # a 和 c 没重跑; x 重跑一次成功
    assert counts == {"a": 1, "x": 2, "c": 1}


def test_retry_non_fan_out():
    runner = PipelineRunner()
    counts = {"n": 0}
    def run(ctx, args):
        counts["n"] += 1
        if counts["n"] == 1:
            raise ZeError("validation_failed", "first try fails")
        return {"ok": True}

    spec = StageSpec(id="x", label="x", run=run)
    run_obj = runner.run([PipelineStep(spec)], {})
    assert run_obj.status == "failed"

    run_obj = runner.retry_failed(run_obj, [PipelineStep(spec)], {})
    assert run_obj.status == "ok"
    assert counts["n"] == 2


def test_retry_no_failed_items_no_op():
    runner = PipelineRunner()
    counts = {"n": 0}
    def run(ctx, args):
        counts["n"] += 1
        return {}

    spec = StageSpec(id="x", label="x", run=run)
    run_obj = runner.run([PipelineStep(spec)], {})
    assert run_obj.status == "ok"

    run_obj = runner.retry_failed(run_obj, [PipelineStep(spec)], {})
    assert run_obj.status == "ok"
    assert counts["n"] == 1   # 没重跑


# ==============================================================
# Pipeline cancel (通过 stage cancel)
# ==============================================================

def test_pipeline_cancelled_item_does_not_block_others():
    """fan_out 中某 item 被 cancel, 其他 item 仍按顺序跑."""
    runner = PipelineRunner()
    def run_item(ctx, args):
        if args["item"] == "cancel-me":
            ctx.cancel()         # 自己标记 cancel
            ctx.checkpoint()      # 触发 CancelledError
        return {"item": args["item"]}

    stage = StageSpec(id="x", label="x", run=run_item)
    run = runner.run(
        [PipelineStep(stage, fan_out=True)],
        {"items": ["a", "cancel-me", "c"]},
    )
    # cancelled 不算 failed, 但默认短路 (failed_items() 不含 cancelled);
    # 所以 a, cancel-me, c 都会跑
    step = run.steps[0]
    statuses = [r.status for r in step.results]
    assert statuses == ["ok", "cancelled", "ok"]
