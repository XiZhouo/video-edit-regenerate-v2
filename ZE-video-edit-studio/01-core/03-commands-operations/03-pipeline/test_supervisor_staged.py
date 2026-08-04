"""test_supervisor_staged — 多 stage 执行路径(P2b)。

覆盖 headline:
  - 多 stage 顺序跑:每 stage 写标记 + 提交 entity.update(逐 stage CAS),全完成 → done。
  - 续跑安全:预置 stage-a 标记 → 只跑 stage-b(不重跑 a)。
  - stage 失败:写 error 标记 + item error;retry_failed 从失败 stage 续跑(不重跑已完成 stage)。
  - cancel:stage 间断点取消 → item pending(可 resume)。
  - 单 stage 路径不受影响由 test_supervisor.py 守。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import artifact_scan as scan  # noqa: E402
from sdk import (  # noqa: E402
    Actor, CommandRegistry, Document, OpLog, RealtimeBroker, ZeError,
    PipelineSupervisor, StageSpec, StagePlan, WorkItem, WorkList,
    register_builtin_commands,
)
from history_authority import HistoryAuthorityError  # noqa: E402

SCHEMA = {"type": "object", "properties": {
    "a": {"type": "boolean"}, "b": {"type": "boolean"}, "title": {"type": "string"}}}


def _make_ctx(tmp_path: Path):
    doc = Document(schema_version="1.0", collaboration_mode="with_save",
                   name="t", schema={"item": SCHEMA})
    broker = RealtimeBroker()
    cmd_reg = CommandRegistry(broker=broker)
    register_builtin_commands(cmd_reg)
    sup = PipelineSupervisor(
        doc=doc, cmd_reg=cmd_reg, op_log=OpLog(), broker=broker,
        data_dir=tmp_path, actor=Actor(user_id="t"), lock=threading.RLock(), save_fn=None,
        allow_legacy_test_adapter=True,
    )
    return doc, sup


def _work_list(run_id="r1", n=1):
    items = [WorkItem(
        item_id=f"g-{i}", entity_type="item", entity_id=f"g-{i}",
        artifacts=(f"art/{run_id}/{i}-a.txt", f"art/{run_id}/{i}-b.txt"),
        payload={"name": "gen", "seed_data": {"title": "gen"}},
    ) for i in range(n)]
    return WorkList(run_id=run_id, name="batch", items=items)


def _two_stage_plan(attempts, flaky_stage=None):
    """stage a → b;每 stage 写自己产物 + 提交一个布尔 patch。flaky_stage 首跑抛错。"""
    def mk(sid, art_idx, patch_key):
        def run(ctx, args):
            iid = args["_item_id"]
            key = f"{iid}::{sid}"
            attempts[key] = attempts.get(key, 0) + 1
            ctx.set_progress(1.0)
            if sid == flaky_stage and attempts[key] == 1:
                raise RuntimeError(f"transient {sid}")
            Path(args["_artifacts"][art_idx]).write_text(f"ok {iid} {sid}")
            return {"patches": {patch_key: True}}
        return StageSpec(id=sid, label=sid.upper(), run=run)
    return StagePlan((mk("a", 0, "a"), mk("b", 1, "b")))


def _replay_plan(
    attempts: dict[str, int],
    *,
    retry_safe: dict[str, bool],
    fail_stage: str | None = None,
) -> StagePlan:
    """无文档写入的三 stage 计数器；用于单独证明 replay 选择而不引入 CAS 干扰。"""
    stages = []
    for stage_id in ("a", "b", "c"):
        def run(ctx, args, sid=stage_id):
            key = f"{args['_item_id']}::{sid}"
            attempts[key] = attempts.get(key, 0) + 1
            if sid == fail_stage:
                raise RuntimeError(f"failed {sid}")
            # 这组测试关注 replay 选择；固定内容让显式 rerun 可幂等核验目标 hash。
            if sid == "a" and args.get("_artifacts"):
                Path(args["_artifacts"][0]).write_text("replay-a")
            if sid == "b" and len(args.get("_artifacts") or ()) > 1:
                Path(args["_artifacts"][1]).write_text("replay-b")
            return {}

        stages.append(StageSpec(
            id=stage_id,
            label=stage_id.upper(),
            run=run,
            retry_safe=retry_safe.get(stage_id, True),
        ))
    return StagePlan(tuple(stages))


def _join(state, timeout=20):
    for t in list(state.threads):
        t.join(timeout)


def test_two_stage_happy_path(tmp_path):
    doc, sup = _make_ctx(tmp_path)
    attempts: dict[str, int] = {}
    wl = _work_list()
    st = sup.start(work_list=wl, stage_plan=_two_stage_plan(attempts))
    _join(st)
    # 两 stage 各跑一次
    assert attempts == {"g-0::a": 1, "g-0::b": 1}
    # 两 stage 标记齐 → item done、run success
    plan = st.stage_plan
    assert scan.stage_item_done(wl.items[0], tmp_path, "r1", plan)
    assert st.status == "success"
    # entity 经两次 entity.update 累积两个 patch
    ent = doc.find("item", "g-0")
    assert ent.data.get("a") is True and ent.data.get("b") is True
    # 运行态:done + stages 全 done
    status = st.item_status["g-0"].to_dict()
    assert status["phase"] == "done"
    assert [s["phase"] for s in status["stages"]] == ["done", "done"]
    # run-view(P2d):多 stage 带每 stage 完成统计 + 已跑完 eta=0、throughput>0
    view = sup.snapshot()[0]
    assert view["stages"] == [
        {"stage_id": "a", "label": "A", "done": 1, "total": 1},
        {"stage_id": "b", "label": "B", "done": 1, "total": 1},
    ]
    assert view.get("eta_sec") == 0.0
    assert view.get("throughput", 0) > 0


def test_cancelled_staged_result_does_not_commit(tmp_path):
    """多 stage 中途 cancel 后,已返回的当前 stage 结果不能继续提交(golden-nugget 909720051 反哺 core)。"""
    doc = Document(schema_version="1.0", collaboration_mode="with_save",
                   name="t", schema={"item": SCHEMA})
    broker = RealtimeBroker()
    cmd_reg = CommandRegistry(broker=broker)
    register_builtin_commands(cmd_reg)
    sup = PipelineSupervisor(
        doc=doc, cmd_reg=cmd_reg, op_log=OpLog(), broker=broker,
        data_dir=tmp_path, actor=Actor(user_id="t"), lock=threading.RLock(), save_fn=None,
        allow_legacy_test_adapter=True,
    )
    entered_b = threading.Event()
    release_b = threading.Event()

    def stage_a(ctx, args):
        return {"patches": {"a": True}}

    def stage_b(ctx, args):
        entered_b.set()
        assert release_b.wait(5)
        return {"patches": {"b": True}}

    wl = _work_list(run_id="cancel-staged")
    st = sup.start(work_list=wl, stage_plan=StagePlan((
        StageSpec(id="a", label="A", run=stage_a),
        StageSpec(id="b", label="B", run=stage_b),
    )))
    assert entered_b.wait(5)
    sup.cancel(st.run_id)
    release_b.set()
    _join(st)

    ent = doc.find("item", "g-0")
    assert ent is not None
    assert ent.data.get("a") is True              # stage-a cancel 前已提交
    assert ent.data.get("b") is not True          # stage-b cancel 后结果被丢弃,未提交


def test_multistage_progress_stays_unknown_while_future_work_is_unmeasured(tmp_path):
    """当前 stage 有真值也不能覆盖未来未知 stage；阶段计数仍独立可见。"""
    _doc, sup = _make_ctx(tmp_path)
    b_started = threading.Event()
    b_report = threading.Event()
    b_reported = threading.Event()
    b_finish = threading.Event()
    c_started = threading.Event()
    c_report = threading.Event()
    c_reported = threading.Event()
    c_finish = threading.Event()

    def stage_a(_ctx, args):
        Path(args["_artifacts"][0]).write_text("a")
        return {}

    def stage_b(ctx, args):
        b_started.set()
        assert b_report.wait(5)
        ctx.set_progress(0.4, "measured-b")
        b_reported.set()
        assert b_finish.wait(5)
        Path(args["_artifacts"][1]).write_text("b")
        return {}

    def stage_c(ctx, args):
        c_started.set()
        assert c_report.wait(5)
        ctx.set_progress(0.5, "measured-c")
        c_reported.set()
        assert c_finish.wait(5)
        Path(args["_artifacts"][2]).write_text("c")
        return {}

    work_list = WorkList(run_id="honest-staged", name="honest", items=[WorkItem(
        item_id="g-0",
        entity_type="item",
        entity_id="g-0",
        artifacts=("art/honest/a.txt", "art/honest/b.txt", "art/honest/c.txt"),
        payload={"name": "gen", "seed_data": {"title": "gen"}},
    )])
    plan = StagePlan((
        StageSpec(id="a", label="A", run=stage_a),
        StageSpec(id="b", label="B", run=stage_b),
        StageSpec(id="c", label="C", run=stage_c),
    ))
    state = sup.start(work_list=work_list, stage_plan=plan)
    assert b_started.wait(2)
    try:
        status = state.item_status["g-0"]
        assert status.progress is None
        assert [stage.progress for stage in status.stages] == [1.0, None, None]
        view = sup.snapshot()[0]
        assert view["progress"] is None
        assert view["stages"] == [
            {"stage_id": "a", "label": "A", "done": 1, "total": 1},
            {"stage_id": "b", "label": "B", "done": 0, "total": 1},
            {"stage_id": "c", "label": "C", "done": 0, "total": 1},
        ]

        b_report.set()
        assert b_reported.wait(2)
        status = state.item_status["g-0"]
        assert status.stages[1].progress == pytest.approx(0.4)
        assert status.progress is None
        assert sup.snapshot()[0]["progress"] is None

        b_finish.set()
        assert c_started.wait(2)
        status = state.item_status["g-0"]
        assert status.progress is None
        assert [stage.progress for stage in status.stages] == [1.0, 1.0, None]

        c_report.set()
        assert c_reported.wait(2)
        expected = (1.0 + 1.0 + 0.5) / 3
        assert state.item_status["g-0"].progress == pytest.approx(expected)
        assert sup.snapshot()[0]["progress"] == pytest.approx(expected)
    finally:
        b_report.set()
        b_finish.set()
        c_report.set()
        c_finish.set()
        _join(state)

    assert state.item_status["g-0"].phase == "done"
    assert state.item_status["g-0"].progress == 1.0
    assert sup.snapshot()[0]["progress"] == 1.0


def test_resume_skips_completed_stage(tmp_path):
    doc, sup = _make_ctx(tmp_path)
    attempts: dict[str, int] = {}
    wl = _work_list()
    # 预置 stage-a 标记 + 产物 → 续跑应只跑 b
    (tmp_path / "art/r1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "art/r1/0-a.txt").write_text("pre")
    scan.mark_stage_done(
        tmp_path, "r1", "g-0", "a", artifact_paths=("art/r1/0-a.txt",),
    )
    st = sup.start(work_list=wl, stage_plan=_two_stage_plan(attempts))
    _join(st)
    assert "g-0::a" not in attempts          # a 未重跑
    assert attempts.get("g-0::b") == 1        # 只跑 b
    assert st.status == "success"


def test_stage_error_then_retry_resumes(tmp_path):
    doc, sup = _make_ctx(tmp_path)
    attempts: dict[str, int] = {}
    wl = _work_list()
    # stage b 首跑失败
    st = sup.start(work_list=wl, stage_plan=_two_stage_plan(attempts, flaky_stage="b"))
    _join(st)
    assert st.status == "error"
    assert scan.error_marker_path(tmp_path, "r1", "g-0").exists()
    assert st.item_status["g-0"].placeholder is True
    assert st.item_status["g-0"].to_dict()["placeholder"] is True
    # a 完成、b 失败 → 完成前缀=1
    assert scan.completed_stage_count(wl.items[0], tmp_path, "r1", st.stage_plan) == 1
    # retry:只重跑失败的 b(a 不重跑)
    sup.retry_failed("r1")
    _join(st)
    assert attempts["g-0::a"] == 1            # a 仍只跑过一次
    assert attempts["g-0::b"] == 2            # b 重跑成功
    assert st.status == "success"
    assert not scan.error_marker_path(tmp_path, "r1", "g-0").exists()
    assert st.item_status["g-0"].placeholder is True


def test_auto_retry_staged_retries_only_failed_stage(tmp_path):
    """F3:多 stage + max_retries=1:stage b 首跑失败 → 自动只重跑 b(a 标记保留不重跑)→ success。"""
    doc, sup = _make_ctx(tmp_path)
    attempts: dict[str, int] = {}
    st = sup.start(work_list=_work_list(), stage_plan=_two_stage_plan(attempts, flaky_stage="b"),
                   max_retries=1)
    _join(st)
    assert st.status == "success"
    assert attempts["g-0::a"] == 1        # a 不重跑(done 标记保留)
    assert attempts["g-0::b"] == 2        # b 自动重跑成功
    assert not scan.error_marker_path(tmp_path, "r1", "g-0").exists()


def test_auto_retry_blocks_retry_unsafe_suffix_with_stable_status(tmp_path):
    """_maybe_retry 遇显式 unsafe stage:不入队，用稳定码代替模糊的原错。"""
    _doc, sup = _make_ctx(tmp_path)
    attempts: dict[str, int] = {}
    plan = _replay_plan(
        attempts,
        retry_safe={"c": False},
        fail_stage="b",
    )
    st = sup.start(work_list=_work_list(), stage_plan=plan, max_retries=3)
    _join(st)

    assert attempts == {"g-0::a": 1, "g-0::b": 1}
    assert st.status == "error"
    error = st.item_status["g-0"].error
    assert error is not None
    assert error["code"] == "pipeline_replay_unsafe"
    assert error["cause"]["code"] == "stage_error"
    assert error["context"]["stages"] == [
        {"item_id": "g-0", "stage_id": "c", "stage_index": 2},
    ]
    assert st.retry_attempts == {}          # 拦截不消耗 retry 计数，也不入队


def test_retry_failed_rejects_retry_unsafe_stage_before_marker_changes(tmp_path):
    """手动 retry_failed 与自动重试共用同一安全语义，被拒时不清 error marker。"""
    _doc, sup = _make_ctx(tmp_path)
    attempts: dict[str, int] = {}
    st = sup.start(
        work_list=_work_list(),
        stage_plan=_replay_plan(
            attempts,
            retry_safe={"b": False},
            fail_stage="b",
        ),
    )
    _join(st)
    marker = scan.error_marker_path(tmp_path, "r1", "g-0")
    before = marker.read_bytes()

    with pytest.raises(ZeError) as exc_info:
        sup.retry_failed("r1")

    assert exc_info.value.code == "pipeline_replay_unsafe"
    assert exc_info.value.context["trigger"] == "retry_failed"
    assert attempts == {"g-0::a": 1, "g-0::b": 1}
    assert marker.read_bytes() == before
    assert st.status == "error"


@pytest.mark.parametrize("entrypoint", ("rerun_step", "run_from_step"))
def test_explicit_step_replay_rejects_unsafe_suffix_before_stale_marking(
    tmp_path,
    entrypoint,
):
    """rerun_step/run_from_step 都在清 done marker 和标 stale 之前检查 suffix。"""
    _doc, sup = _make_ctx(tmp_path)
    attempts: dict[str, int] = {}
    st = sup.start(
        work_list=_work_list(),
        stage_plan=_replay_plan(attempts, retry_safe={"c": False}),
    )
    _join(st)
    assert st.status == "success"
    before = dict(attempts)
    marker_bytes = {
        stage_id: scan.stage_marker_path(tmp_path, "r1", "g-0", stage_id).read_bytes()
        for stage_id in ("a", "b", "c")
    }

    with pytest.raises(ZeError) as exc_info:
        getattr(sup, entrypoint)("r1", stage_id="b", item_id="g-0")

    assert exc_info.value.code == "pipeline_replay_unsafe"
    assert exc_info.value.context["trigger"] == entrypoint
    assert exc_info.value.context["stages"] == [
        {"item_id": "g-0", "stage_id": "c", "stage_index": 2},
    ]
    assert attempts == before
    assert st.status == "success"
    assert {
        stage_id: scan.stage_marker_path(tmp_path, "r1", "g-0", stage_id).read_bytes()
        for stage_id in ("a", "b", "c")
    } == marker_bytes


@pytest.mark.parametrize("entrypoint", ("rerun_step", "run_from_step"))
def test_explicit_step_replay_checks_only_selected_suffix(tmp_path, entrypoint):
    """从 b 开始时只检查/执行 b..end；上游 a 即使 unsafe 也不误拦。"""
    _doc, sup = _make_ctx(tmp_path)
    attempts: dict[str, int] = {}
    st = sup.start(
        work_list=_work_list(),
        stage_plan=_replay_plan(attempts, retry_safe={"a": False}),
    )
    _join(st)

    getattr(sup, entrypoint)("r1", stage_id="b", item_id="g-0")
    _join(st)

    assert st.status == "success"
    assert attempts == {"g-0::a": 1, "g-0::b": 2, "g-0::c": 2}


def test_recover_restores_max_retries(tmp_path):
    """F3:manifest 带 max_retries=1 → recover 重建后 resume,flaky stage 仍享自动重试 → success。"""
    import json as _json
    wl = _work_list()
    plan0 = _two_stage_plan({})
    rd = tmp_path / ".pipelines" / "r1"
    (rd / "status").mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(_json.dumps(
        {**wl.to_dict(), "stages": plan0.to_meta(), "concurrency": 1, "max_retries": 1}))
    (tmp_path / "art/r1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "art/r1/0-a.txt").write_text("a")
    scan.mark_stage_done(
        tmp_path, "r1", "g-0", "a", artifact_paths=("art/r1/0-a.txt",),
    )

    doc, sup = _make_ctx(tmp_path)
    sup._cmd_reg.dispatch("entity.add",
                          {"type": "item", "id": "g-0", "name": "gen", "data": {"a": True}},
                          actor=Actor(user_id="t"), source="system", doc=doc, op_log=sup._op_log)
    rebuilt: dict[str, int] = {}
    sup._stage_resolver = lambda manifest: _two_stage_plan(rebuilt, flaky_stage="b")
    assert sup.recover() == ["r1"]
    st = sup._runs["r1"]
    assert st.max_retries == 1                   # manifest 恢复了重试上限
    sup.resume("r1")
    _join(st)
    assert st.status == "success"
    assert rebuilt.get("g-0::b") == 2            # b 首跑失败 → 恢复的 max_retries 生效自动重跑
    assert "g-0::a" not in rebuilt               # a 不重跑(标记在)


def test_commit_failure_at_later_stage_marks_error(tmp_path):
    """stage a 提交成功(标记+entity 更新),stage b 提交失败(非法 patch → entity.update ZeError)
    → item error、b 未标记、完成前缀=1。覆盖 _commit_stage 的错误返回分支(CAS 链)。"""
    doc, sup = _make_ctx(tmp_path)
    wl = _work_list()

    def run_a(ctx, args):
        Path(args["_artifacts"][0]).write_text("a")
        return {"patches": {"a": True}}

    def run_b(ctx, args):
        Path(args["_artifacts"][1]).write_text("b")
        return {"patches": {"a": "not-a-boolean"}}   # 违反 schema(a 应为 bool)→ 提交失败

    plan = StagePlan((StageSpec(id="a", label="A", run=run_a),
                      StageSpec(id="b", label="B", run=run_b)))
    st = sup.start(work_list=wl, stage_plan=plan)
    _join(st)
    assert st.status == "error"
    assert scan.error_marker_path(tmp_path, "r1", "g-0").exists()
    # a 标记在、b 没有(提交失败前 break,不写 b 标记)
    assert scan.completed_stage_count(wl.items[0], tmp_path, "r1", st.stage_plan) == 1
    status = st.item_status["g-0"].to_dict()
    assert status["phase"] == "error"
    assert status["error"] is not None
    # a 的成功提交已落:entity.a == True
    assert doc.find("item", "g-0").data.get("a") is True
    # b 原始输出只留在 attempt 供诊断；commit 失败不能把最终文件/receipt 伪装成完成。
    assert (tmp_path / "art/r1/0-a.txt").exists()
    assert not (tmp_path / "art/r1/0-b.txt").exists()
    assert list((tmp_path / ".pipelines/r1/attempts/g-0/b").glob("*/0001-0-b.txt"))
    assert not scan.stage_marker_path(tmp_path, "r1", "g-0", "b").exists()
    assert not scan.stage_item_done(wl.items[0], tmp_path, "r1", st.stage_plan)


def test_staged_history_unknown_becomes_replay_blocking_repair_marker(tmp_path):
    """_commit_stage must preserve typed unknown outcome instead of losing its worker."""
    _doc, sup = _make_ctx(tmp_path)
    work_list = _work_list(run_id="history-staged")
    release = threading.Event()
    calls: list[str] = []

    def run_a(_ctx, args):
        calls.append("a")
        assert release.wait(5)
        Path(args["_artifacts"][0]).write_text("a")
        return {"patches": {"a": True}}

    def run_b(_ctx, args):
        calls.append("b")
        Path(args["_artifacts"][1]).write_text("b")
        return {"patches": {"b": True}}

    class OutcomeUnknownCoordinator:
        def execute(self, producer, trace_id=""):
            producer()
            raise HistoryAuthorityError(
                "history_append_outcome_indeterminate",
                "append acknowledgement was lost",
            )

    state = sup.start(
        work_list=work_list,
        stage_plan=StagePlan((
            StageSpec(id="a", label="A", run=run_a),
            StageSpec(id="b", label="B", run=run_b),
        )),
        max_retries=1,
    )
    sup._commit_coordinator = OutcomeUnknownCoordinator()
    release.set()
    _join(state)

    marker = scan.read_json_file(
        scan.error_marker_path(tmp_path, "history-staged", "g-0"),
    )
    assert state.status == "error"
    assert calls == ["a"]
    assert state.retry_attempts == {}
    assert marker["code"] == "commit_repair_required"
    assert marker["context"]["outcome_unknown"] is True
    assert marker["context"]["repair_required"] is True
    assert marker["context"]["retry_safe"] is False
    assert not scan.stage_marker_path(
        tmp_path, "history-staged", "g-0", "a",
    ).exists()
    with pytest.raises(ZeError) as retry:
        sup.retry_failed("history-staged")
    assert retry.value.code == "commit_repair_required"
    assert calls == ["a"]


def test_start_requires_exactly_one_stage_arg(tmp_path):
    from entity import ZeError
    doc, sup = _make_ctx(tmp_path)
    wl = _work_list()
    try:
        sup.start(work_list=wl)
        assert False, "expected ZeError"
    except ZeError as e:
        assert e.code == "pipeline_start_requires_stage"
    attempts: dict[str, int] = {}
    plan = _two_stage_plan(attempts)
    try:
        sup.start(work_list=wl, item_stage=plan[0], stage_plan=plan)
        assert False, "expected ZeError"
    except ZeError as e:
        assert e.code == "pipeline_start_ambiguous_stage"


# ==============================================================
# worker 池并发(P2c-A)
# ==============================================================

import threading as _threading  # noqa: E402


def test_concurrency_runs_items_in_parallel(tmp_path):
    """concurrency=N + threading.Barrier(N):仅当 N 个 worker 真并行才放行;
    顺序执行 → barrier 超时 BrokenBarrier → item error。强证 fan-out 并发。"""
    doc, sup = _make_ctx(tmp_path)
    N = 4
    barrier = _threading.Barrier(N, timeout=8)

    def run(ctx, args):
        barrier.wait()                       # N 个 worker 同时到此才继续
        Path(args["_artifacts"][0]).write_text("ok")
        return {"patches": {"a": True}}

    plan = StagePlan((StageSpec(id="g", label="G", run=run),))
    items = [WorkItem(item_id=f"i{i}", entity_type="item", entity_id=f"i{i}",
                      artifacts=(f"art/p/{i}.txt",)) for i in range(N)]
    wl = WorkList(run_id="rc", name="c", items=items)
    st = sup.start(work_list=wl, stage_plan=plan, concurrency=N)
    _join(st)
    assert st.status == "success"
    assert all(scan.item_done(it, tmp_path, "rc", "g") for it in items)


def test_pool_drains_more_items_than_workers(tmp_path):
    """concurrency=3 + 7 item:work-stealing 抽干队列,全部完成(不丢项、不重复)。"""
    doc, sup = _make_ctx(tmp_path)
    runs: dict[str, int] = {}
    lock = _threading.Lock()

    def run(ctx, args):
        with lock:
            runs[args["_item_id"]] = runs.get(args["_item_id"], 0) + 1
        Path(args["_artifacts"][0]).write_text("ok")
        return {"patches": {"a": True}}

    plan = StagePlan((StageSpec(id="g", label="G", run=run),))
    items = [WorkItem(item_id=f"i{i}", entity_type="item", entity_id=f"i{i}",
                      artifacts=(f"art/d/{i}.txt",)) for i in range(7)]
    wl = WorkList(run_id="rd", name="d", items=items)
    st = sup.start(work_list=wl, stage_plan=plan, concurrency=3)
    _join(st)
    assert st.status == "success"
    assert sum(1 for it in items if scan.item_done(it, tmp_path, "rd", "g")) == 7
    assert all(runs[f"i{i}"] == 1 for i in range(7))   # 每项恰跑一次(无重复/丢失)


def test_resume_on_finished_run_is_noop(tmp_path):
    """已终态(success)的 run 再 resume → 不重起池、不重跑、状态不变(护 TOCTOU 双开修复)。"""
    doc, sup = _make_ctx(tmp_path)
    attempts: dict[str, int] = {}
    wl = _work_list()
    st = sup.start(work_list=wl, stage_plan=_two_stage_plan(attempts))
    _join(st)
    assert st.status == "success"
    before = dict(attempts)
    sup.resume("r1")
    _join(st)
    assert attempts == before        # 没重跑任何 stage
    assert st.status == "success"


def test_resource_gate_caps_stage_concurrency(tmp_path):
    """注册 max_concurrency=2 的资源闸门 + 6-way 并发:声明该 scope 的 stage 峰值并发被压到 ≤ 2。"""
    import time as _time
    from sdk import ResourceContract, GateRegistry
    reg = GateRegistry()
    reg.get_or_create(ResourceContract(resource="X", scope="cpu.test", max_concurrency=2))

    doc = Document(schema_version="1.0", collaboration_mode="with_save", name="t",
                   schema={"item": SCHEMA})
    broker = RealtimeBroker()
    cmd_reg = CommandRegistry(broker=broker)
    register_builtin_commands(cmd_reg)
    sup = PipelineSupervisor(
        doc=doc, cmd_reg=cmd_reg, op_log=OpLog(), broker=broker, data_dir=tmp_path,
        actor=Actor(user_id="t"), lock=threading.RLock(), save_fn=None, gate_registry=reg,
        allow_legacy_test_adapter=True)

    active = {"now": 0, "peak": 0}
    lk = _threading.Lock()

    def run(ctx, args):
        with lk:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        _time.sleep(0.05)                 # 制造重叠窗口
        with lk:
            active["now"] -= 1
        Path(args["_artifacts"][0]).write_text("ok")
        return {"patches": {"a": True}}

    plan = StagePlan((StageSpec(id="g", label="G", run=run, resource_scopes=("cpu.test",)),))
    items = [WorkItem(item_id=f"i{i}", entity_type="item", entity_id=f"i{i}",
                      artifacts=(f"art/g/{i}.txt",)) for i in range(6)]
    wl = WorkList(run_id="rg", name="g", items=items)
    st = sup.start(work_list=wl, stage_plan=plan, concurrency=6)
    _join(st)
    assert st.status == "success"
    assert 1 <= active["peak"] <= 2       # 6-way 并发被闸门压到 ≤ 2


def test_capacity_gate_cost_caps_concurrency(tmp_path):
    """capacity=10 + 每 stage cost=4 → 加权 sum≤10 把 6-way 并发压到 ≤ 2(2*4=8≤10,3*4=12>10)。
    证 resource_costs 真用上(若 cost 默认 1,10 容量可放 6 并发,peak 会到 6)。"""
    import time as _time
    from sdk import ResourceContract, GateRegistry
    reg = GateRegistry()
    reg.get_or_create(ResourceContract(resource="VRAM", scope="mem.vram",
                                       max_concurrency=8, capacity=10))
    doc = Document(schema_version="1.0", collaboration_mode="with_save", name="t",
                   schema={"item": SCHEMA})
    broker = RealtimeBroker()
    cmd_reg = CommandRegistry(broker=broker)
    register_builtin_commands(cmd_reg)
    sup = PipelineSupervisor(
        doc=doc, cmd_reg=cmd_reg, op_log=OpLog(), broker=broker, data_dir=tmp_path,
        actor=Actor(user_id="t"), lock=threading.RLock(), save_fn=None, gate_registry=reg,
        allow_legacy_test_adapter=True)

    active = {"now": 0, "peak": 0}
    lk = _threading.Lock()

    def run(ctx, args):
        with lk:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        _time.sleep(0.05)
        with lk:
            active["now"] -= 1
        Path(args["_artifacts"][0]).write_text("ok")
        return {"patches": {"a": True}}

    plan = StagePlan((StageSpec(id="g", label="G", run=run,
                                resource_scopes=("mem.vram",),
                                resource_costs={"mem.vram": 4}),))
    items = [WorkItem(item_id=f"i{i}", entity_type="item", entity_id=f"i{i}",
                      artifacts=(f"art/cap/{i}.txt",)) for i in range(6)]
    wl = WorkList(run_id="rcap", name="cap", items=items)
    st = sup.start(work_list=wl, stage_plan=plan, concurrency=6)
    _join(st)
    assert st.status == "success"
    assert 1 <= active["peak"] <= 2


def test_recover_rebuilds_paused_run_then_resume_completes(tmp_path):
    """模拟重启:磁盘有 manifest + stage-a 标记(b 未跑)+ 持久占位 entity。
    新 supervisor.recover()(带 stage_resolver)→ run 重建为 paused;resume 续跑 b → success。"""
    import json as _json
    wl = _work_list()
    plan0 = _two_stage_plan({})
    # --- 铺「上次进程」留下的磁盘状态 ---
    rd = tmp_path / ".pipelines" / "r1"
    (rd / "status").mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(_json.dumps(
        {**wl.to_dict(), "stages": plan0.to_meta(), "concurrency": 1}))
    (tmp_path / "art/r1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "art/r1/0-a.txt").write_text("a")
    scan.mark_stage_done(
        tmp_path, "r1", "g-0", "a", artifact_paths=("art/r1/0-a.txt",),
    )

    # --- 新进程:doc 已持久化占位 entity(a 完成态)+ supervisor 带 resolver ---
    doc, sup = _make_ctx(tmp_path)
    sup._cmd_reg.dispatch("entity.add",
                          {"type": "item", "id": "g-0", "name": "gen", "data": {"a": True}},
                          actor=Actor(user_id="t"), source="system", doc=doc, op_log=sup._op_log)

    rebuilt: dict[str, int] = {}
    sup._stage_resolver = lambda manifest: _two_stage_plan(rebuilt)   # mod 钩子:据 manifest 重建计划

    recovered = sup.recover()
    assert recovered == ["r1"]
    st = sup._runs["r1"]
    assert st.status == "paused"

    sup.resume("r1")
    _join(st)
    assert st.status == "success"
    assert rebuilt.get("g-0::b") == 1            # 只续跑 b
    assert "g-0::a" not in rebuilt               # a 不重跑(标记在)
    assert scan.stage_item_done(wl.items[0], tmp_path, "r1", st.stage_plan)


def test_recovered_pending_unsafe_stage_cannot_resume(tmp_path):
    """重启后无 marker 的 stage 可能已产生外部副作用；retry_safe=False 时不猜测重跑。"""
    import json as _json

    wl = _work_list()
    manifest_plan = _replay_plan({}, retry_safe={"b": False})
    rd = tmp_path / ".pipelines" / "r1"
    (rd / "status").mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(_json.dumps(
        {**wl.to_dict(), "stages": manifest_plan.to_meta(), "concurrency": 1},
    ))
    (tmp_path / "art/r1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "art/r1/0-a.txt").write_text("replay-a")
    scan.mark_stage_done(
        tmp_path, "r1", "g-0", "a", artifact_paths=("art/r1/0-a.txt",),
    )

    doc, sup = _make_ctx(tmp_path)
    sup._cmd_reg.dispatch(
        "entity.add",
        {"type": "item", "id": "g-0", "name": "gen", "data": {}},
        actor=Actor(user_id="t"), source="system", doc=doc, op_log=sup._op_log,
    )
    attempts: dict[str, int] = {}
    sup._stage_resolver = lambda _manifest: _replay_plan(
        attempts, retry_safe={"b": False},
    )
    assert sup.recover() == ["r1"]
    st = sup._runs["r1"]

    with pytest.raises(ZeError) as exc_info:
        sup.resume("r1")

    assert exc_info.value.code == "pipeline_replay_unsafe"
    assert exc_info.value.context["trigger"] == "resume"
    assert exc_info.value.context["stages"][0]["stage_id"] == "b"
    assert attempts == {}
    assert st.status == "paused"
    assert st.threads == []
    assert st.pause_gate.is_set() is False


def test_recovered_resume_checks_only_unfinished_suffix(tmp_path):
    """已完成前缀 a 即使 unsafe 也不会被重放；恢复只续跑安全的 b/c。"""
    import json as _json

    wl = _work_list()
    manifest_plan = _replay_plan({}, retry_safe={"a": False})
    rd = tmp_path / ".pipelines" / "r1"
    (rd / "status").mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(_json.dumps(
        {**wl.to_dict(), "stages": manifest_plan.to_meta(), "concurrency": 1},
    ))
    (tmp_path / "art/r1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "art/r1/0-a.txt").write_text("replay-a")
    scan.mark_stage_done(
        tmp_path, "r1", "g-0", "a", artifact_paths=("art/r1/0-a.txt",),
    )

    doc, sup = _make_ctx(tmp_path)
    sup._cmd_reg.dispatch(
        "entity.add",
        {"type": "item", "id": "g-0", "name": "gen", "data": {}},
        actor=Actor(user_id="t"), source="system", doc=doc, op_log=sup._op_log,
    )
    attempts: dict[str, int] = {}
    sup._stage_resolver = lambda _manifest: _replay_plan(
        attempts, retry_safe={"a": False},
    )
    assert sup.recover() == ["r1"]
    st = sup._runs["r1"]

    sup.resume("r1")
    _join(st)

    assert st.status == "success"
    assert attempts == {"g-0::b": 1, "g-0::c": 1}
    assert st.replay_guard_items == set()


def test_recover_noop_without_resolver(tmp_path):
    """无 stage_resolver → recover() 是 no-op(框架自身零影响)。"""
    wl = _work_list()
    rd = tmp_path / ".pipelines" / "r1"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(WorkList(run_id="r1", items=[]).to_dict().__repr__())
    doc, sup = _make_ctx(tmp_path)                # 无 resolver
    assert sup.recover() == []


def test_recover_skips_corrupt_manifest_without_aborting(tmp_path):
    """坏 manifest(非法 concurrency)→ 该 run 跳过、不抛 → recover 不被拖垮。"""
    import json as _json
    rd = tmp_path / ".pipelines" / "bad"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(_json.dumps(
        {"run_id": "bad", "name": "b", "items": [], "concurrency": "not-an-int"}))
    doc, sup = _make_ctx(tmp_path)
    sup._stage_resolver = lambda m: _two_stage_plan({})
    assert sup.recover() == []          # 坏 run 被跳过,无异常
    assert "bad" not in sup._runs
    failure = next(view for view in sup.snapshot() if view["id"] == "bad")
    assert failure["recovery_required"] is True
    assert failure["recovery_error"] == {"code": "pipeline_manifest_invalid"}
    assert failure["log_lines"] == ["recovery_required"]


@pytest.mark.parametrize("manifest_run_id", (None, "other"))
def test_recover_requires_manifest_run_id_to_match_its_directory(tmp_path, manifest_run_id):
    import json as _json

    rd = tmp_path / ".pipelines" / "bound-run"
    rd.mkdir(parents=True, exist_ok=True)
    manifest = WorkList(run_id="bound-run", items=[]).to_dict()
    if manifest_run_id is None:
        manifest.pop("run_id")
    else:
        manifest["run_id"] = manifest_run_id
    (rd / "run.json").write_text(_json.dumps(manifest))
    _doc, sup = _make_ctx(tmp_path)
    sup._stage_resolver = lambda _manifest: _two_stage_plan({})

    assert sup.recover() == []
    assert "bound-run" not in sup._runs
    failure = next(view for view in sup.snapshot() if view["id"] == "bound-run")
    assert failure["recovery_error"] == {"code": "pipeline_run_identity_mismatch"}


@pytest.mark.skipif(not hasattr(__import__("os"), "O_NOFOLLOW"), reason="requires nofollow")
def test_recover_never_follows_or_accepts_multiply_linked_manifest_paths(tmp_path):
    import json as _json
    import os as _os

    pipelines = tmp_path / ".pipelines"
    pipelines.mkdir()
    payload = _json.dumps(WorkList(run_id="placeholder", items=[]).to_dict())

    symlink_run = pipelines / "symlink-file"
    symlink_run.mkdir()
    symlink_source = tmp_path / "symlink-source.json"
    symlink_source.write_text(payload.replace("placeholder", "symlink-file"))
    (symlink_run / "run.json").symlink_to(symlink_source)

    hardlink_run = pipelines / "hardlink-file"
    hardlink_run.mkdir()
    hardlink_source = tmp_path / "hardlink-source.json"
    hardlink_source.write_text(payload.replace("placeholder", "hardlink-file"))
    _os.link(hardlink_source, hardlink_run / "run.json")

    outside_dir = tmp_path / "outside-run"
    outside_dir.mkdir()
    (outside_dir / "run.json").write_text(
        payload.replace("placeholder", "symlink-directory"),
    )
    (pipelines / "symlink-directory").symlink_to(outside_dir, target_is_directory=True)

    _doc, sup = _make_ctx(tmp_path)
    sup._stage_resolver = lambda _manifest: _two_stage_plan({})

    assert sup.recover() == []
    failures = {view["id"]: view for view in sup.snapshot()}
    assert failures["symlink-file"]["recovery_error"] == {
        "code": "pipeline_manifest_invalid",
    }
    assert failures["hardlink-file"]["recovery_error"] == {
        "code": "pipeline_manifest_invalid",
    }
    assert "symlink-directory" not in failures


def test_repeated_recover_keeps_one_live_view_and_drops_disappeared_failures(tmp_path):
    import json as _json
    import shutil as _shutil

    live = tmp_path / ".pipelines" / "live"
    live.mkdir(parents=True)
    (live / "run.json").write_text(_json.dumps(
        {**WorkList(run_id="live", items=[]).to_dict(), "concurrency": 1},
    ))
    bad = tmp_path / ".pipelines" / "bad"
    bad.mkdir()
    (bad / "run.json").write_text("{")

    _doc, sup = _make_ctx(tmp_path)
    sup._stage_resolver = lambda _manifest: _two_stage_plan({})
    assert sup.recover() == ["live"]
    assert [(view["id"], view["status"]) for view in sup.snapshot()] == [
        ("live", "paused"),
        ("bad", "error"),
    ]

    # A later broken manifest cannot shadow the already registered in-memory run.
    (live / "run.json").write_text("{")
    assert sup.recover() == []
    assert [view["id"] for view in sup.snapshot()].count("live") == 1
    assert next(view for view in sup.snapshot() if view["id"] == "live")["status"] == "paused"

    # Recovery failures are a projection of the current scan, not an append-only ghost ledger.
    _shutil.rmtree(bad)
    assert sup.recover() == []
    assert "bad" not in {view["id"] for view in sup.snapshot()}


def test_breakpoint_pause_after_halts_then_resume(tmp_path):
    """「关闭」断点(StageSpec.pause_after):stage a 完成后 run 停在边界(b 未跑、run paused),
    resume 续跑 b → done。无断点路径由 test_two_stage_happy_path 守(back-compat 字节等价)。"""
    doc, sup = _make_ctx(tmp_path)
    attempts: dict[str, int] = {}
    base = _two_stage_plan(attempts)
    a, b = base[0], base[1]
    plan = StagePlan((StageSpec(id=a.id, label=a.label, run=a.run, pause_after=True), b))
    st = sup.start(work_list=_work_list(), stage_plan=plan)
    # halt 后 worker park 在 b 前的 pause_gate.wait()(不退出),故轮询 paused 而非 join。
    deadline = time.time() + 20
    while st.status != "paused" and time.time() < deadline:
        time.sleep(0.02)
    assert st.status == "paused"
    assert attempts.get("g-0::a") == 1
    assert "g-0::b" not in attempts          # 在 a 之后停住,b 未跑
    sup.resume("r1")
    _join(st)
    assert attempts.get("g-0::b") == 1       # resume 后续跑 b
    assert st.status == "success"


def test_breakpoint_resume_can_first_run_retry_unsafe_stage(tmp_path):
    """retry_safe 管的是「重放」，不是禁止 unsafe stage 在正常暂停后首次执行。"""
    _doc, sup = _make_ctx(tmp_path)
    attempts: dict[str, int] = {}
    base = _replay_plan(attempts, retry_safe={"b": False})
    a, b, c = base.stages
    plan = StagePlan((
        StageSpec(id=a.id, label=a.label, run=a.run, pause_after=True),
        b,
        c,
    ))
    st = sup.start(work_list=_work_list(), stage_plan=plan)
    deadline = time.time() + 20
    while st.status != "paused" and time.time() < deadline:
        time.sleep(0.02)

    assert st.status == "paused"
    assert st.replay_guard_items == set()
    sup.resume("r1")
    _join(st)

    assert st.status == "success"
    assert attempts == {"g-0::a": 1, "g-0::b": 1, "g-0::c": 1}


def test_set_breakpoint_toggles_set(tmp_path):
    """运行时 set_breakpoint:halt=True 加入断点集、halt=False 移除(开启)。"""
    doc, sup = _make_ctx(tmp_path)
    st = sup.start(work_list=_work_list(), stage_plan=_two_stage_plan({}))
    _join(st)
    sup.set_breakpoint("r1", stage_id="b", halt=True)
    assert "b" in st.breakpoints
    sup.set_breakpoint("r1", stage_id="b", halt=False)
    assert "b" not in st.breakpoints


def test_snapshot_exposes_breakpoints_and_stage_count(tmp_path):
    """前端 toggle 的数据契约:snapshot 暴露 breakpoints(运行时集)+ stage_count(>1 才显 toggle)。"""
    doc, sup = _make_ctx(tmp_path)
    st = sup.start(work_list=_work_list(), stage_plan=_two_stage_plan({}))
    _join(st)
    view = sup.snapshot()[0]
    assert view["stage_count"] == 2          # 两 stage → 前端会在非末步显 toggle
    assert view["breakpoints"] == []         # 未设断点 → 空集
    sup.set_breakpoint("r1", stage_id="a", halt=True)
    assert sup.snapshot()[0]["breakpoints"] == ["a"]   # set 后 snapshot 同步反映 armed 态
