"""test_supervisor — PipelineSupervisor 回归测试(Block1 加载/管线/占位系统).

覆盖 headline:占位在开始即建 + 进度按文件系统派生 + 错误标记 + 统一重试只重跑失败项 → 成功。
跑法(从本叶子目录): python -m pytest test_supervisor.py -q
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]   # ZE-skill-build/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdk import (  # noqa: E402
    Actor, CommandRegistry, CommandSpec, Document, Entity, OpLog, Patch, RealtimeBroker,
    PipelineSupervisor, StageSpec, WorkItem, WorkList, new_operation,
    ZeError, register_builtin_commands,
)
from sdk.test_commit_surface_matrix import assert_commit_order  # noqa: E402
from history_authority import HistoryAuthorityError  # noqa: E402

ITEM_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"}, "done": {"type": "boolean"}}}


def test_stage_ports_consume_sealed_registration_projection():
    registry = CommandRegistry()
    spec = CommandSpec(
        id="sealed.stage",
        label="sealed",
        kind="stage",
        ui_ports={
            "inputs": [{"id": "original-input", "label": "Input"}],
            "outputs": [{"id": "original-output", "label": "Output"}],
        },
        execute=lambda **kwargs: new_operation(
            command_id="sealed.stage",
            source=kwargs["source"],
            actor=kwargs["actor"].to_dict(),
            patches=(),
            reverse_patches=(),
        ),
    )
    registry.register(spec)
    spec.ui_ports["inputs"][0]["id"] = "forged-input"
    spec.ui_ports["outputs"][0]["id"] = "forged-output"
    projection = registry.registered_declaration("sealed.stage")
    projection["ui_ports"]["inputs"][0]["id"] = "forged-projection"

    supervisor = PipelineSupervisor.__new__(PipelineSupervisor)
    supervisor._cmd_reg = registry
    stage = SimpleNamespace(
        id="fallback-stage",
        command_id="sealed.stage",
        input_ports=(),
        output_ports=(),
    )

    assert supervisor._stage_ports(stage, "input") == [
        {"id": "original-input", "label": "Input"},
    ]
    assert supervisor._stage_ports(stage, "output") == [
        {"id": "original-output", "label": "Output"},
    ]


def test_supervisor_requires_commit_coordinator_by_default(tmp_path):
    """公开构造默认不得静默退回自行 save/append/publish 的旁路。"""
    with pytest.raises(ZeError) as exc_info:
        PipelineSupervisor(
            doc=SimpleNamespace(),
            cmd_reg=CommandRegistry(),
            op_log=OpLog(),
            broker=RealtimeBroker(),
            data_dir=tmp_path,
            actor=Actor(user_id="u"),
            lock=threading.RLock(),
        )

    assert exc_info.value.code == "commit_coordinator_required"


def _make_ctx(tmp_path: Path, *, lifecycle_guard=None):
    doc = Document(schema_version="1.0", collaboration_mode="with_save",
                   name="t", schema={"item": ITEM_SCHEMA})
    broker = RealtimeBroker()
    cmd_reg = CommandRegistry(broker=broker)
    register_builtin_commands(cmd_reg)
    sup = PipelineSupervisor(
        doc=doc, cmd_reg=cmd_reg, op_log=OpLog(), broker=broker,
        data_dir=tmp_path, actor=Actor(user_id="t"), lock=threading.RLock(), save_fn=None,
        allow_legacy_test_adapter=True,
        lifecycle_guard=lifecycle_guard,
    )
    return doc, sup


def _work_list(run_id="r1", n=3, flaky=1):
    items = []
    for i in range(n):
        eid = f"g-{i}"
        items.append(WorkItem(
            item_id=eid, entity_type="item", entity_id=eid,
            artifacts=(f"art/{run_id}/{i}.txt",),
            payload={"name": "gen", "seed_data": {"title": "gen", "done": False},
                     "title": f"item {i}", "_flaky": (i == flaky)},
        ))
    return WorkList(run_id=run_id, name="batch", items=items)


def _stage(attempts):
    """item 1 首跑失败、重跑成功(用 attempts dict 计次);其余直接成功。写产物文件。"""
    def run(ctx, args):
        iid = args["_item_id"]
        attempts[iid] = attempts.get(iid, 0) + 1
        ctx.log("info", "stage attempt", attempt=attempts[iid])
        ctx.set_progress(1.0)
        if args.get("_flaky") and attempts[iid] == 1:
            raise RuntimeError("transient")
        Path(args["_artifacts"][0]).write_text(f"ok {iid} @{attempts[iid]}")
        return {"name": args.get("title"), "patches": {"title": args.get("title"), "done": True}}
    return StageSpec(id="gen", label="gen", run=run)


def _join(state, timeout=20):
    for t in list(state.threads):
        t.join(timeout)


class _LifecycleProbe:
    """Thread-local lease probe used by real background worker tests."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.local = threading.local()

    @property
    def active(self) -> bool:
        return bool(getattr(self.local, "active", False))

    @contextmanager
    def __call__(self):
        assert self.active is False
        self.local.active = True
        self.events.append("guard-enter")
        try:
            yield
        finally:
            assert self.active is True
            self.events.append("guard-exit")
            self.local.active = False


def test_worker_holds_lifecycle_guard_from_stage_through_commit_and_receipt(
    tmp_path,
    monkeypatch,
):
    """Every item owns one uninterrupted generation lease through finalization."""
    events: list[str] = []
    lease = _LifecycleProbe(events)
    doc, sup = _make_ctx(tmp_path, lifecycle_guard=lease)
    entered = threading.Event()
    release = threading.Event()

    def run(_ctx, args):
        assert lease.active is True
        item_id = args["_item_id"]
        events.append(f"stage:{item_id}")
        entered.set()
        assert release.wait(5)
        Path(args["_artifacts"][0]).write_text(f"artifact:{item_id}")
        return {"patches": {"title": f"done:{item_id}", "done": True}}

    state = sup.start(
        work_list=_work_list(run_id="guarded-items", n=2, flaky=-1),
        item_stage=StageSpec(id="guarded", label="guarded", run=run),
        concurrency=1,
    )
    assert entered.wait(2)

    class GuardAwareCoordinator:
        def execute(self, producer, trace_id=""):
            assert lease.active is True
            produced = producer()
            if produced is None:
                operations = ()
            elif hasattr(produced, "operations"):
                operations = tuple(produced.operations)
            else:
                operations = (produced,)
            assert len(operations) == 1
            item_id = operations[0].patches[0].entity_id
            events.append(f"commit:{item_id}")
            return SimpleNamespace(
                receipt=SimpleNamespace(commit_id=f"commit-{item_id}"),
                is_committed=True,
                operations=operations,
            )

    sup._commit_coordinator = GuardAwareCoordinator()
    import artifact_scan as scan
    real_finalize = scan.finalize_attempt

    def guarded_finalize(attempt, **kwargs):
        assert lease.active is True
        events.append(f"finalize:{attempt.item_id}")
        return real_finalize(attempt, **kwargs)

    monkeypatch.setattr(scan, "finalize_attempt", guarded_finalize)
    release.set()
    _join(state)

    assert state.status == "success"
    assert events == [
        "guard-enter",
        "stage:g-0",
        "commit:g-0",
        "finalize:g-0",
        "guard-exit",
        "guard-enter",
        "stage:g-1",
        "commit:g-1",
        "finalize:g-1",
        "guard-exit",
    ]
    assert all(entity.data.get("done") for entity in doc.list("item"))


def test_worker_backstop_persists_typed_history_reload_marker(tmp_path, monkeypatch):
    """A raw authority failure cannot terminate a worker without an item marker."""
    import artifact_scan as scan

    _doc, sup = _make_ctx(tmp_path)

    def fail_before_item_commit(_state, _item):
        raise HistoryAuthorityError(
            "history_head_changed",
            "diagnostic detail must not become the public marker message",
        )

    monkeypatch.setattr(sup, "_process_item_single", fail_before_item_commit)
    state = sup.start(
        work_list=_work_list(run_id="history-worker", n=1, flaky=-1),
        item_stage=StageSpec(id="gen", label="gen", run=lambda _ctx, _args: {}),
    )
    _join(state)

    marker = scan.read_json_file(
        scan.error_marker_path(tmp_path, "history-worker", "g-0"),
    )
    assert state.status == "error"
    assert state.item_status["g-0"].error == marker
    assert marker == {
        "code": "history_authority_mismatch",
        "message": "canonical history changed; reload the project before retrying",
        "trace_id": "history-worker",
        "context": {
            "reason": "history_head_changed",
            "durability_phase": "preflight",
            "outcome_unknown": False,
            "retry_safe": False,
            "repair_required": False,
            "reload_required": True,
        },
    }


@pytest.mark.parametrize(
    ("reason", "outcome_unknown", "expected_code"),
    (
        ("history_head_changed", True, "commit_repair_required"),
        ("history_suffix_mismatch", False, "history_authority_mismatch"),
    ),
)
def test_history_marker_classification_uses_typed_fact_not_reason_text(
    reason,
    outcome_unknown,
    expected_code,
):
    error = HistoryAuthorityError(
        reason,
        "classification decoy",
        outcome_unknown=outcome_unknown,
        durability_phase="test_phase",
    )

    projected = PipelineSupervisor._history_authority_failure(
        SimpleNamespace(trace_id="typed"),
        error,
    )

    assert projected["code"] == expected_code
    assert projected["context"]["outcome_unknown"] is outcome_unknown


def test_shutdown_returns_false_while_uncooperative_worker_is_alive(tmp_path):
    """Delete/reset callers must not mistake a bounded join timeout for quiescence."""
    _doc, sup = _make_ctx(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def uncooperative_stage(_ctx, _args):
        entered.set()
        release.wait(5)  # Intentionally ignores the StageContext cancellation flag.
        return {}

    state = sup.start(
        work_list=_work_list(run_id="shutdown-timeout", n=1, flaky=-1),
        item_stage=StageSpec(id="blocking", label="blocking", run=uncooperative_stage),
    )
    assert entered.wait(2)
    try:
        assert sup.shutdown(join_timeout=0.02) is False
        assert any(thread.is_alive() for thread in state.threads)
    finally:
        release.set()
        _join(state)

    assert sup.shutdown(join_timeout=0.5) is True


def test_single_stage_progress_is_unknown_until_reported_and_done_is_one(tmp_path):
    """起跑不是 0%；只有 stage callback 产生数值，完成态固定为 1.0。"""
    _doc, sup = _make_ctx(tmp_path)
    started = threading.Event()
    allow_report = threading.Event()
    reported = threading.Event()
    finish = threading.Event()

    def run(ctx, args):
        started.set()
        assert allow_report.wait(5)
        ctx.set_progress(0.4, "measured")
        reported.set()
        assert finish.wait(5)
        Path(args["_artifacts"][0]).write_text("done")
        return {"patches": {"done": True}}

    state = sup.start(
        work_list=_work_list(run_id="honest-single", n=1, flaky=-1),
        item_stage=StageSpec(id="measured", label="measured", run=run),
    )
    assert started.wait(2)
    try:
        status = state.item_status["g-0"]
        assert status.phase == "running"
        assert status.progress is None
        assert status.placeholder is True
        assert status.to_dict()["placeholder"] is True
        assert sup.snapshot()[0]["progress"] is None

        allow_report.set()
        assert reported.wait(2)
        assert state.item_status["g-0"].progress == pytest.approx(0.4)
        assert sup.snapshot()[0]["progress"] == pytest.approx(0.4)
    finally:
        finish.set()
        _join(state)

    assert state.item_status["g-0"].phase == "done"
    assert state.item_status["g-0"].progress == 1.0
    assert state.item_status["g-0"].placeholder is True
    assert sup.snapshot()[0]["progress"] == 1.0


def test_run_progress_requires_every_unfinished_item_to_have_real_progress(tmp_path):
    """完成计数独立存在；任一未完成 item 未报告时，run 汇总保持 unknown。"""
    _doc, sup = _make_ctx(tmp_path)
    started = {item_id: threading.Event() for item_id in ("g-0", "g-1")}
    allow_report = {item_id: threading.Event() for item_id in started}
    reported = {item_id: threading.Event() for item_id in started}
    finish = threading.Event()

    def run(ctx, args):
        item_id = args["_item_id"]
        started[item_id].set()
        assert allow_report[item_id].wait(5)
        ctx.set_progress(0.25 if item_id == "g-0" else 0.75)
        reported[item_id].set()
        assert finish.wait(5)
        Path(args["_artifacts"][0]).write_text(item_id)
        return {"patches": {"done": True}}

    state = sup.start(
        work_list=_work_list(run_id="honest-run", n=2, flaky=-1),
        item_stage=StageSpec(id="measured", label="measured", run=run),
        concurrency=2,
    )
    assert all(event.wait(2) for event in started.values())
    try:
        allow_report["g-0"].set()
        assert reported["g-0"].wait(2)
        view = sup.snapshot()[0]
        assert view["stage"] == "0/2"
        assert view["progress"] is None

        allow_report["g-1"].set()
        assert reported["g-1"].wait(2)
        assert sup.snapshot()[0]["progress"] == pytest.approx(0.5)
    finally:
        finish.set()
        for event in allow_report.values():
            event.set()
        _join(state)

    view = sup.snapshot()[0]
    assert view["stage"] == "2/2"
    assert view["progress"] == 1.0


def test_stage_creates_entities(tmp_path):
    """反哺 D:stage 返回 created_entities → entity.batch_add 单 Operation 建 N 个新实体。"""
    doc, sup = _make_ctx(tmp_path)

    def run(ctx, args):
        Path(args["_artifacts"][0]).write_text("ok")
        return {"created_entities": [
            {"type": "item", "name": "新A", "data": {"title": "A", "done": False}},
            {"type": "item", "name": "新B", "data": {"title": "B", "done": False}},
        ]}

    wl = WorkList(run_id="rc", name="create", items=[WorkItem(
        item_id="g-0", entity_type="item", entity_id="g-0", artifacts=("art/rc/0.txt",),
        payload={"name": "gen", "seed_data": {"title": "seed", "done": False}})])
    state = sup.start(work_list=wl, item_stage=StageSpec(id="c", label="c", run=run))
    _join(state)
    after = doc.list("item")
    created = [e for e in after if e.name in ("新A", "新B")]
    assert len(created) == 2                             # stage 经 entity.batch_add 真建了这 2 个
    assert all(not e.deleted for e in created)


def test_stage_created_entities_on_conflict_skip(tmp_path):
    """created_entities_on_conflict='skip' 透传 batch_add:确定性 id 已存在→跳过不炸
    (D3 自动建帧 TOCTOU 根治,3.55.0)。"""
    doc, sup = _make_ctx(tmp_path)
    doc.add(Entity(type="item", id="det-1", name="已在", data={"title": "老", "done": False}))

    def run(ctx, args):
        Path(args["_artifacts"][0]).write_text("ok")
        return {"created_entities": [
            {"type": "item", "id": "det-1", "name": "撞车", "data": {"title": "新", "done": False}},
            {"type": "item", "id": "det-2", "name": "新建", "data": {"title": "B", "done": False}},
        ], "created_entities_on_conflict": "skip"}

    wl = WorkList(run_id="rski", name="skip", items=[WorkItem(
        item_id="g-0", entity_type="item", entity_id="g-0", artifacts=("art/rski/0.txt",),
        payload={"name": "gen", "seed_data": {"title": "seed", "done": False}})])
    state = sup.start(work_list=wl, item_stage=StageSpec(id="c", label="c", run=run))
    _join(state)
    assert doc.get("item", "det-1").data["title"] == "老"     # 未被覆盖
    assert doc.find("item", "det-2") is not None
    view = sup.snapshot()[0]
    assert view["progress"] == 1.0 and not view.get("errors")  # item 不再显示失败


def test_cancelled_stage_result_does_not_commit(tmp_path):
    """长 stage 被 cancel 后即使返回 ok,也不能再写 Document(golden-nugget 909720051 反哺 core)。"""
    doc = Document(schema_version="1.0", collaboration_mode="with_save",
                   name="t", schema={"item": ITEM_SCHEMA})
    broker = RealtimeBroker()
    cmd_reg = CommandRegistry(broker=broker)
    register_builtin_commands(cmd_reg)
    sup = PipelineSupervisor(
        doc=doc, cmd_reg=cmd_reg, op_log=OpLog(), broker=broker,
        data_dir=tmp_path, actor=Actor(user_id="t"), lock=threading.RLock(), save_fn=None,
        allow_legacy_test_adapter=True,
    )
    entered = threading.Event()
    release = threading.Event()

    def run(ctx, args):
        entered.set()
        assert release.wait(5)
        return {"patches": {"done": True}}

    wl = WorkList(run_id="cancel-after-stage", name="cancel", items=[WorkItem(
        item_id="g-0", entity_type="item", entity_id="g-0",
        payload={"name": "gen", "seed_data": {"title": "gen", "done": False}},
    )])
    state = sup.start(work_list=wl, item_stage=StageSpec(id="slow", label="slow", run=run))
    assert entered.wait(5)
    sup.cancel(state.run_id)
    release.set()
    _join(state)

    ent = doc.find("item", "g-0")
    assert ent is not None
    assert ent.data.get("done") is False          # cancel 后 ok 结果被丢弃,未提交


def test_placeholders_progress_error_retry(tmp_path):
    doc, sup = _make_ctx(tmp_path)
    attempts = {}
    wl = _work_list()
    state = sup.start(work_list=wl, item_stage=_stage(attempts))

    # 占位在 start 时就建好(3 个 entity 立刻存在)
    assert len(doc.list("item")) == 3

    _join(state)

    # 文件系统派生:2 个产物落盘,1 个失败
    runs = sup.snapshot()
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "error"
    assert run["failed_count"] == 1
    assert run["stage"] == "2/3"
    # 2/3 是完成项计数，不是工作量百分比；失败项没有可完成的真进度。
    assert run["progress"] is None
    assert run["detail_version"] == 1
    assert any(e["message"] == "stage attempt" and e.get("item_id") == "g-0"
               for e in run["log_events"])
    assert any(e["level"] == "error" and e.get("item_id") == "g-1"
               for e in run["log_events"])
    assert any("[error] g-1@gen:" in line or "[error] g-1:" in line
               for line in run["log_lines"])
    assert any(i["item_id"] == "g-1" and i["phase"] == "error"
               for i in run["active_items"])
    assert any(a["item_id"] == "g-0" and a["present"] is True
               for a in run["artifacts"])
    assert any(a["item_id"] == "g-1" and a["present"] is False
               for a in run["artifacts"])

    # 错误项有运行态;成功项 entity 已被 source=ai 填充 done
    statuses = sup.entity_status_snapshot()
    assert [s["phase"] for s in statuses] == ["error"]
    assert statuses[0]["placeholder"] is True
    assert state.item_status["g-1"].placeholder is True
    done_items = [e for e in doc.list("item") if e.data.get("done")]
    assert len(done_items) == 2

    # 统一重试:只重跑失败项 → 成功
    sup.retry_failed(run["id"])
    _join(state)
    run2 = sup.snapshot()[0]
    assert run2["status"] == "success"
    assert run2["failed_count"] == 0
    assert run2["stage"] == "3/3"
    assert all(e.data.get("done") for e in doc.list("item"))
    # 失败项重跑了第 2 次(attempts==2),成功项没被重跑(仍 ==1)—— 精确重试
    assert attempts["g-1"] == 2
    assert attempts["g-0"] == 1 and attempts["g-2"] == 1
    assert state.item_status["g-1"].placeholder is True


def test_recover_restores_placeholder_identity_and_legacy_manifest_fails_open_false(tmp_path):
    import json
    from stage_plan import StagePlan

    def write_pending_run(run_id: str, *, placeholder_ids=None):
        item = WorkItem(
            item_id=f"{run_id}-item",
            entity_type="item",
            entity_id=f"{run_id}-entity",
            artifacts=(f"art/{run_id}/0.txt",),
            payload={},
        )
        work_list = WorkList(run_id=run_id, name=run_id, items=[item])
        run_dir = tmp_path / ".pipelines" / run_id
        (run_dir / "status").mkdir(parents=True)
        manifest = {**work_list.to_dict(), "concurrency": 1}
        if placeholder_ids is not None:
            manifest["placeholder_item_ids"] = placeholder_ids
        (run_dir / "run.json").write_text(json.dumps(manifest))
        return item

    marked_item = write_pending_run(
        "marked",
        # 未知 id 和非 exact-str 值都不得被恢复为占位身份。
        placeholder_ids=["marked-item", "unknown-item", 1],
    )
    legacy_item = write_pending_run("legacy")

    _doc, sup = _make_ctx(tmp_path)
    sup._stage_resolver = lambda _manifest: StagePlan.from_single(
        StageSpec(id="gen", label="gen", run=lambda _ctx, _args: {}),
    )
    assert sup.recover() == ["legacy", "marked"]

    marked = sup._runs["marked"]
    assert marked.placeholder_item_ids == {"marked-item"}
    assert marked.item_status[marked_item.item_id].placeholder is True
    assert marked.item_status[marked_item.item_id].to_dict()["placeholder"] is True
    # 恢复后再重建 phase 也不丢 marker。
    sup._set_item(marked, marked_item, "running", stage_label="gen")
    assert marked.item_status[marked_item.item_id].placeholder is True
    sup._set_item(
        marked, marked_item, "error",
        error={"code": "stage_error", "message": "failed"},
    )
    assert marked.item_status[marked_item.item_id].placeholder is True

    legacy = sup._runs["legacy"]
    assert legacy.placeholder_item_ids == set()
    assert legacy.item_status[legacy_item.item_id].placeholder is False
    assert "placeholder" not in legacy.item_status[legacy_item.item_id].to_dict()


def test_recover_binds_manifest_identity_fields_to_the_durable_document(tmp_path):
    """Changing payload+seed together must not redirect a recovered run to another input."""
    import json
    from stage_plan import StagePlan

    doc, sup = _make_ctx(tmp_path)
    for run_id in ("identity-good", "identity-tampered"):
        doc.add(Entity(
            type="item", id=f"{run_id}-entity", name=run_id,
            data={"title": "durable-source", "done": False},
        ))

    def write_run(run_id: str, source: str) -> None:
        item = WorkItem(
            item_id=f"{run_id}-item",
            entity_type="item",
            entity_id=f"{run_id}-entity",
            artifacts=(f"art/{run_id}/0.txt",),
            payload={
                "seed_data": {"title": source, "done": False},
                "source": source,
                "recovery_identity_fields": ["title"],
            },
        )
        run_dir = tmp_path / ".pipelines" / run_id
        (run_dir / "status").mkdir(parents=True)
        manifest = {
            **WorkList(run_id=run_id, name=run_id, items=[item]).to_dict(),
            "concurrency": 1,
            "placeholder_item_ids": [item.item_id],
        }
        (run_dir / "run.json").write_text(json.dumps(manifest))

    write_run("identity-good", "durable-source")
    # Both the stage payload and its manifest seed are changed coherently.  The committed entity
    # remains the second authority and must make this run unrecoverable.
    write_run("identity-tampered", "other-source")
    sup._stage_resolver = lambda _manifest: StagePlan.from_single(
        StageSpec(id="gen", label="gen", run=lambda _ctx, _args: {}),
    )

    assert sup.recover() == ["identity-good"]
    assert "identity-tampered" not in sup._runs
    rejected = next(view for view in sup.snapshot() if view["id"] == "identity-tampered")
    assert rejected["status"] == "error"
    assert rejected["recovery_required"] is True
    assert rejected["recovery_error"] == {"code": "pipeline_recovery_identity_mismatch"}
    assert rejected["can_resume"] is False
    assert rejected["failed_count"] == 0


def test_prepare_start_rejects_identity_fields_that_disagree_with_existing_entity(tmp_path):
    doc, sup = _make_ctx(tmp_path)
    doc.add(Entity(
        type="item", id="existing", name="Existing",
        data={"title": "durable-source", "done": False},
    ))
    item = WorkItem(
        item_id="existing", entity_type="item", entity_id="existing",
        payload={
            "seed_data": {"title": "other-source", "done": False},
            "recovery_identity_fields": ["title"],
        },
    )

    with pytest.raises(ZeError) as captured:
        sup.start(
            work_list=WorkList(run_id="identity-start", items=[item]),
            item_stage=StageSpec(id="gen", label="gen", run=lambda _ctx, _args: {}),
        )
    assert captured.value.code == "pipeline_recovery_identity_mismatch"


def test_auto_retry_flaky_item_succeeds(tmp_path):
    """F3:max_retries=1 → flaky 项首跑失败自动重试、第二次成功 → run success(无需手动 retry)。"""
    import json
    doc, sup = _make_ctx(tmp_path)
    attempts = {}
    wl = _work_list(run_id="r3")
    state = sup.start(work_list=wl, item_stage=_stage(attempts), max_retries=1)
    _join(state)
    run = sup.snapshot()[0]
    assert run["status"] == "success"
    assert run["failed_count"] == 0
    assert run["stage"] == "3/3"
    # flaky 项自动重跑第 2 次;成功项没被重跑
    assert attempts["g-1"] == 2
    assert attempts["g-0"] == 1 and attempts["g-2"] == 1
    # 结构化重试日志 "retry n/N"
    assert any(e.get("code") == "auto_retry" and e.get("item_id") == "g-1"
               and e["message"] == "retry 1/1" for e in run["log_events"])
    # manifest 持久化 max_retries(供 _recover_one 恢复后仍生效)
    manifest = json.loads((tmp_path / ".pipelines/r3/run.json").read_text())
    assert manifest["max_retries"] == 1


def test_max_retries_zero_keeps_current_behavior(tmp_path):
    """F3 回归护栏:max_retries=0(显式传 = 默认)不自动重试 → flaky 项落 error(现行为)。"""
    doc, sup = _make_ctx(tmp_path)
    attempts = {}
    wl = _work_list(run_id="r4")
    state = sup.start(work_list=wl, item_stage=_stage(attempts), max_retries=0)
    _join(state)
    run = sup.snapshot()[0]
    assert run["status"] == "error"
    assert run["failed_count"] == 1
    assert attempts["g-1"] == 1          # 未自动重跑


def test_auto_retry_exhausted_lands_error(tmp_path):
    """F3:超过 max_retries 才落 error;手动 retry_failed 不看自动计数、不受影响。"""
    doc, sup = _make_ctx(tmp_path)
    attempts = {}

    def run(ctx, args):
        iid = args["_item_id"]
        attempts[iid] = attempts.get(iid, 0) + 1
        raise RuntimeError("always fails")

    wl = _work_list(run_id="r5", n=1, flaky=-1)
    state = sup.start(work_list=wl, item_stage=StageSpec(id="gen", label="gen", run=run),
                      max_retries=1)
    _join(state)
    view = sup.snapshot()[0]
    assert view["status"] == "error"
    assert view["failed_count"] == 1
    assert attempts["g-0"] == 2          # 首跑 + 1 次自动重试,不再多试
    # 手动 retry_failed 不看自动计数:仍可再跑(失败后也不会再进自动重试循环)
    sup.retry_failed("r5")
    _join(state)
    assert attempts["g-0"] == 3
    assert sup.snapshot()[0]["status"] == "error"


def test_resume_skips_only_artifact_with_valid_done_receipt(tmp_path):
    """重开只跳过 receipt+checksum 有效项；裸文件不再是完成证明。"""
    doc, sup = _make_ctx(tmp_path)
    attempts = {}
    wl = _work_list(run_id="r2", n=2, flaky=-1)  # 无 flaky
    # 预置 item 0 的产物(模拟上轮已产出)
    art0 = tmp_path / "art/r2/0.txt"
    art0.parent.mkdir(parents=True, exist_ok=True)
    art0.write_text("preexisting")
    import artifact_scan as scan
    scan.mark_item_done(
        tmp_path, "r2", "g-0", "gen", artifact_paths=("art/r2/0.txt",),
    )
    state = sup.start(work_list=wl, item_stage=_stage(attempts))
    _join(state)
    run = sup.snapshot()[0]
    assert run["status"] == "success" and run["stage"] == "2/2"
    # item 0 被跳过(没进 stage),item 1 跑了一次
    assert "g-0" not in attempts
    assert attempts.get("g-1") == 1


def test_stage_output_stays_attempt_when_document_commit_is_indeterminate(tmp_path):
    """即使 stage 已写出字节，非 committed receipt 也绝不能 promote/mark done。"""
    doc, sup = _make_ctx(tmp_path)

    class _IndeterminateCoordinator:
        def execute(self, _producer, trace_id=""):
            receipt = SimpleNamespace(
                status=SimpleNamespace(value="indeterminate"),
                to_dict=lambda: {"status": "indeterminate", "trace_id": trace_id},
            )
            return SimpleNamespace(receipt=receipt, is_committed=False, operations=())

    wl = _work_list(run_id="ri", n=1, flaky=-1)
    gate = threading.Event()

    def run(_ctx, args):
        gate.wait(5)
        Path(args["_artifacts"][0]).write_text("ok g-0")
        return {"patches": {"title": "generated", "done": True}}

    state = sup.start(work_list=wl, item_stage=StageSpec(id="gen", label="gen", run=run))
    sup._commit_coordinator = _IndeterminateCoordinator()
    gate.set()
    _join(state)

    item = wl.items[0]
    assert state.status == "error"
    assert not (tmp_path / item.artifacts[0]).exists()
    attempts = list((tmp_path / ".pipelines/ri/attempts/g-0/gen").glob("*/0000-0.txt"))
    assert len(attempts) == 1 and attempts[0].read_text().startswith("ok g-0")
    import artifact_scan as scan
    assert not scan.item_marker_path(tmp_path, "ri", "g-0").exists()
    assert not scan.item_done(item, tmp_path, "ri", "gen")


def test_history_outcome_unknown_blocks_auto_manual_and_recovered_replay(tmp_path):
    """Post-write history uncertainty is a durable repair stop, never a retry signal."""
    import artifact_scan as scan
    from stage_plan import StagePlan

    _doc, sup = _make_ctx(tmp_path)
    release = threading.Event()
    stage_calls: dict[str, int] = {}

    def run(_ctx, args):
        stage_calls[args["_item_id"]] = stage_calls.get(args["_item_id"], 0) + 1
        assert release.wait(5)
        Path(args["_artifacts"][0]).write_text("unknown")
        return {"patches": {"done": True}}

    class OutcomeUnknownCoordinator:
        def execute(self, producer, trace_id=""):
            producer()  # The typed classification, not this test text, says replay is unsafe.
            raise HistoryAuthorityError(
                "history_suffix_mismatch",
                "post-write verification failed",
            ).with_commit_facts({
                "phase": "store_write",
                "outcome": "indeterminate",
                "commit_id": "commit-unknown",
                "retry_safe": False,
            })

    work_list = _work_list(run_id="history-unknown", n=1, flaky=-1)
    state = sup.start(
        work_list=work_list,
        item_stage=StageSpec(id="gen", label="gen", run=run),
        max_retries=2,
    )
    sup._commit_coordinator = OutcomeUnknownCoordinator()
    release.set()
    _join(state)

    marker_path = scan.error_marker_path(tmp_path, "history-unknown", "g-0")
    marker = scan.read_json_file(marker_path)
    assert state.status == "error"
    assert stage_calls == {"g-0": 1}
    assert state.retry_attempts == {}
    assert marker["code"] == "commit_repair_required"
    assert marker["context"] == {
        "reason": "history_suffix_mismatch",
        "durability_phase": "post_write_verification",
        "outcome_unknown": True,
        "retry_safe": False,
        "repair_required": True,
        "reload_required": False,
        "commit_facts": {
            "phase": "store_write",
            "outcome": "indeterminate",
            "commit_id": "commit-unknown",
            "retry_safe": False,
        },
    }
    with pytest.raises(ZeError) as current_retry:
        sup.retry_failed("history-unknown")
    assert current_retry.value.code == "commit_repair_required"
    assert current_retry.value.context["outcome_unknown"] is True
    assert scan.read_json_file(marker_path) == marker

    # A fresh supervisor is a real reload boundary, but an unknown result still
    # has no replay grant.  Recovery exposes the error and leaves the stage cold.
    doc2, recovered_sup = _make_ctx(tmp_path)
    doc2.add(Entity(
        type="item", id="g-0", name="gen",
        data={"title": "gen", "done": False},
    ))
    recovered_calls: dict[str, int] = {}
    recovered_sup._stage_resolver = lambda _manifest: StagePlan.from_single(
        _stage(recovered_calls),
    )
    assert recovered_sup.recover() == ["history-unknown"]
    recovered = recovered_sup._runs["history-unknown"]
    assert recovered.status == "error"
    assert recovered.history_reload_ready_items == set()
    assert recovered_calls == {}
    with pytest.raises(ZeError) as recovered_retry:
        recovered_sup.retry_failed("history-unknown")
    assert recovered_retry.value.code == "commit_repair_required"
    assert recovered_calls == {}
    assert scan.read_json_file(marker_path) == marker


def test_history_prewrite_mismatch_requires_reload_then_allows_explicit_retry(tmp_path):
    """Safe pre-write mismatch cannot auto-retry in a stale Context; reload is explicit."""
    import artifact_scan as scan
    from stage_plan import StagePlan

    _doc, sup = _make_ctx(tmp_path)
    release = threading.Event()
    calls: dict[str, int] = {}

    def run(_ctx, args):
        calls[args["_item_id"]] = calls.get(args["_item_id"], 0) + 1
        assert release.wait(5)
        Path(args["_artifacts"][0]).write_text("prewrite")
        return {"patches": {"done": True}}

    class PrewriteMismatchCoordinator:
        def execute(self, _producer, trace_id=""):
            raise HistoryAuthorityError(
                "history_head_changed",
                "canonical head changed before producer",
            )

    work_list = _work_list(run_id="history-reload", n=1, flaky=-1)
    state = sup.start(
        work_list=work_list,
        item_stage=StageSpec(id="gen", label="gen", run=run),
        max_retries=2,
    )
    sup._commit_coordinator = PrewriteMismatchCoordinator()
    release.set()
    _join(state)

    marker_path = scan.error_marker_path(tmp_path, "history-reload", "g-0")
    marker = scan.read_json_file(marker_path)
    assert calls == {"g-0": 1}
    assert state.retry_attempts == {}
    assert marker["code"] == "history_authority_mismatch"
    assert marker["context"]["reload_required"] is True
    assert marker["context"]["outcome_unknown"] is False
    assert any(
        event.get("code") == "auto_retry_skipped_history_reload"
        for event in state.log_events
    )
    with pytest.raises(ZeError) as stale_retry:
        sup.retry_failed("history-reload")
    assert stale_retry.value.code == "history_authority_mismatch"
    assert stale_retry.value.context["reload_required"] is True

    # A new Context may consume the exact safe marker once, but only after an
    # explicit retry request; recover itself never starts a worker.
    doc2, recovered_sup = _make_ctx(tmp_path)
    doc2.add(Entity(
        type="item", id="g-0", name="gen",
        data={"title": "gen", "done": False},
    ))
    recovered_calls: dict[str, int] = {}
    recovered_sup._stage_resolver = lambda _manifest: StagePlan.from_single(
        _stage(recovered_calls),
    )
    assert recovered_sup.recover() == ["history-reload"]
    recovered = recovered_sup._runs["history-reload"]
    assert recovered.status == "error"
    assert recovered.history_reload_ready_items == {"g-0"}
    assert recovered_calls == {}

    recovered_sup.retry_failed("history-reload")
    _join(recovered)
    assert recovered.status == "success"
    assert recovered.history_reload_ready_items == set()
    assert recovered_calls == {"g-0": 1}
    assert not marker_path.exists()


def test_recover_repairs_committed_checkpoint_and_clears_completion_error(tmp_path, monkeypatch):
    """checkpoint 已证明 completion intent 时，重启可补 promote/done 并收敛旧收尾错误。"""
    import json
    import artifact_scan as scan
    from stage_plan import StagePlan

    wl = _work_list(run_id="rr", n=1, flaky=-1)
    item = wl.items[0]
    run_dir = tmp_path / ".pipelines/rr"
    (run_dir / "status").mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({**wl.to_dict(), "concurrency": 1}))
    attempt = scan.begin_attempt(item, tmp_path, "rr", "gen", single_stage=True)
    attempt.staging_paths[0].write_text("repair")
    real_promote = scan._promote_checkpoint
    monkeypatch.setattr(scan, "_promote_checkpoint", lambda *_args: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        scan.finalize_attempt(
            attempt, commit_id="artifact-only:c", commit_kind="artifact_only",
            operation_ids=(), require_all=True,
        )
    monkeypatch.setattr(scan, "_promote_checkpoint", real_promote)
    scan.atomic_write_json(scan.error_marker_path(tmp_path, "rr", item.item_id), {
        "code": "pipeline_completion_repair_required", "message": "temporary fs error",
    })

    doc, sup = _make_ctx(tmp_path)
    sup._cmd_reg.dispatch(
        "entity.add",
        {"type": "item", "id": item.entity_id, "name": "gen",
         "data": {"title": "seed", "done": False}},
        actor=Actor(user_id="t"), source="system", doc=doc, op_log=None,
    )
    sup._stage_resolver = lambda _manifest: StagePlan.from_single(_stage({}))
    assert sup.recover() == ["rr"]
    assert sup._runs["rr"].status == "success"
    assert scan.item_done(item, tmp_path, "rr", "gen")
    assert not scan.error_marker_path(tmp_path, "rr", item.item_id).exists()


def test_live_worker_repairs_checkpoint_and_clears_stale_completion_error(
    tmp_path, monkeypatch,
):
    """Repair must converge during the same process, not only after recover()."""
    import artifact_scan as scan

    wl = _work_list(run_id="rlive", n=1, flaky=-1)
    item = wl.items[0]
    attempt = scan.begin_attempt(item, tmp_path, "rlive", "gen", single_stage=True)
    attempt.staging_paths[0].write_text("repair-live")
    real_promote = scan._promote_checkpoint
    monkeypatch.setattr(
        scan, "_promote_checkpoint",
        lambda *_args: (_ for _ in ()).throw(OSError("x")),
    )
    with pytest.raises(OSError):
        scan.finalize_attempt(
            attempt, commit_id="artifact-only:c", commit_kind="artifact_only",
            operation_ids=(), require_all=True,
        )
    monkeypatch.setattr(scan, "_promote_checkpoint", real_promote)
    scan.atomic_write_json(scan.error_marker_path(tmp_path, "rlive", item.item_id), {
        "code": "pipeline_completion_repair_required", "message": "temporary fs error",
    })

    _doc, sup = _make_ctx(tmp_path)
    calls: dict[str, int] = {}
    state = sup.start(work_list=wl, item_stage=_stage(calls))
    _join(state)

    assert state.status == "success"
    assert calls == {}
    assert scan.item_done(item, tmp_path, "rlive", "gen")
    assert not scan.error_marker_path(tmp_path, "rlive", item.item_id).exists()


# ==============================================================
# 并发双 run 圈同一实体:CAS 输家的 error 占位被成功产出取代(supersede)
# ==============================================================

def _make_ctx_full(tmp_path: Path):
    """同 _make_ctx,但把 cmd_reg/op_log 也交出来(测试要在 stage 外/内模拟第三方写入)。"""
    doc = Document(schema_version="1.0", collaboration_mode="with_save",
                   name="t", schema={"item": ITEM_SCHEMA})
    broker = RealtimeBroker()
    cmd_reg = CommandRegistry(broker=broker)
    register_builtin_commands(cmd_reg)
    op_log = OpLog()
    sup = PipelineSupervisor(
        doc=doc, cmd_reg=cmd_reg, op_log=op_log, broker=broker,
        data_dir=tmp_path, actor=Actor(user_id="t"), lock=threading.RLock(), save_fn=None,
        allow_legacy_test_adapter=True,
    )
    return doc, sup, cmd_reg, op_log, broker


def _one_item_wl(run_id: str, eid: str = "e-1"):
    return WorkList(run_id=run_id, name=run_id, items=[WorkItem(
        item_id=f"{run_id}-0", entity_type="item", entity_id=eid,
        artifacts=(f"art/{run_id}/0.txt",),
        payload={"name": "gen", "seed_data": {"title": "seed", "done": False},
                 "title": f"by {run_id}"})])


def _cas_losing_stage(doc, cmd_reg, op_log):
    """stage:产物写成功,但生成期间实体被第三方 update(版本 bump)→ commit 必 CAS 冲突。"""
    def run(ctx, args):
        ent = doc.find("item", "e-1")
        cmd_reg.dispatch(
            "entity.update",
            {"type": "item", "id": "e-1", "patches": {"title": "外部改"},
             "expected_version": ent.version},
            actor=Actor(user_id="someone"), source="user", doc=doc, op_log=op_log)
        Path(args["_artifacts"][0]).write_text("ok")
        return {"patches": {"title": args.get("title"), "done": True}}
    return StageSpec(id="gen", label="gen", run=run)


def test_cas_error_superseded_by_done_is_hidden(tmp_path):
    """事故复刻:实体已被 run A 成功产出 → run B 输 CAS 落 error,占位不上卡。

    run 详情保留真实 error(审计);entity_status 快照跳过;SSE 广播端把该 error
    改发 done(前端按实体键删占位)。
    """
    doc, sup, cmd_reg, op_log, broker = _make_ctx_full(tmp_path)
    payloads: list[dict] = []
    broker.subscribe(callback=lambda p: payloads.append(p))

    # run A:正常成功 → done 台账记下 ("item","e-1")
    state_a = sup.start(work_list=_one_item_wl("ra"),
                        item_stage=_stage({}))
    _join(state_a)
    assert doc.find("item", "e-1").data.get("done") is True

    # run B:同一实体,生成期间被第三方改版本 → commit CAS 冲突 → item error
    state_b = sup.start(work_list=_one_item_wl("rb"),
                        item_stage=_cas_losing_stage(doc, cmd_reg, op_log))
    _join(state_b)

    # run 详情:真实 error 保留(不粉饰)
    view_b = next(r for r in sup.snapshot() if r["id"] == "rb")
    assert view_b["status"] == "error" and view_b["failed_count"] == 1

    # 占位快照:e-1 无任何条目(error 被 done 取代,done 本就不占位)
    assert all(s["entity_id"] != "e-1" for s in sup.entity_status_snapshot())

    # SSE 广播:rb 关于 e-1 的最后一条 entity_status 是 done(清占位),不是 error
    statuses = [s for p in payloads if p.get("type") == "entity_status"
                for s in p.get("statuses", []) if s.get("run_id") == "rb"]
    assert statuses and statuses[-1]["phase"] == "done"
    assert statuses[-1]["error"] is None


def test_cas_error_without_done_stays_visible(tmp_path):
    """反例保护:实体从未成功产出(如用户生成期间改 prompt)→ error 占位必须可见。"""
    doc, sup, cmd_reg, op_log, broker = _make_ctx_full(tmp_path)
    payloads: list[dict] = []
    broker.subscribe(callback=lambda p: payloads.append(p))

    state = sup.start(work_list=_one_item_wl("rb"),
                      item_stage=_cas_losing_stage(doc, cmd_reg, op_log))
    _join(state)

    snap = [s for s in sup.entity_status_snapshot() if s["entity_id"] == "e-1"]
    assert len(snap) == 1 and snap[0]["phase"] == "error"
    assert snap[0]["error"]["code"] == "cas_version_mismatch"
    statuses = [s for p in payloads if p.get("type") == "entity_status"
                for s in p.get("statuses", []) if s.get("entity_id") == "e-1"]
    assert statuses[-1]["phase"] == "error"


def test_active_entity_ids_tracks_inflight_only(tmp_path):
    """active_entity_ids:活跃 run 的 pending/running 实体在列;跑完即出列。"""
    doc, sup = _make_ctx(tmp_path)
    gate = threading.Event()

    def run(ctx, args):
        gate.wait(20)
        Path(args["_artifacts"][0]).write_text("ok")
        return {"patches": {"done": True}}

    wl = _work_list(run_id="rg", n=3, flaky=-1)
    state = sup.start(work_list=wl, item_stage=StageSpec(id="gen", label="gen", run=run))
    try:
        deadline = time.time() + 5
        while time.time() < deadline and len(sup.active_entity_ids()) < 3:
            time.sleep(0.02)
        # worker 阻塞在第一个 item(running),其余排队(pending)→ 3 个全在列
        assert sup.active_entity_ids() == {("item", "g-0"), ("item", "g-1"), ("item", "g-2")}
    finally:
        gate.set()
    _join(state)
    assert sup.snapshot()[0]["status"] == "success"
    assert sup.active_entity_ids() == set()   # 终态 run 不再占用


def test_recover_supersedes_stale_cas_error(tmp_path):
    """部署重启路径:磁盘上输家 run 的 error marker 还在,但同一实体已被赢家 run 产出
    → recover 重建台账,快照不再吐这条 error(红色不随重启复活)。"""
    import json as _json
    from stage_plan import StagePlan

    def _write_run(rid: str):
        wl = WorkList(run_id=rid, name=rid, items=[WorkItem(
            item_id=f"{rid}-0", entity_type="item", entity_id="e-1",
            artifacts=(f"art/{rid}/0.txt",), payload={})])
        rd = tmp_path / ".pipelines" / rid
        (rd / "status").mkdir(parents=True, exist_ok=True)
        (rd / "run.json").write_text(_json.dumps({**wl.to_dict(), "concurrency": 1}))
        return rd

    # 赢家 ra:有效 receipt → done;输家 rb:error marker → error
    _write_run("ra")
    (tmp_path / "art/ra").mkdir(parents=True, exist_ok=True)
    (tmp_path / "art/ra/0.txt").write_text("won")
    import artifact_scan as _scan
    _scan.mark_item_done(
        tmp_path, "ra", "ra-0", "gen", artifact_paths=("art/ra/0.txt",),
    )
    rd_b = _write_run("rb")
    (rd_b / "status" / "rb-0.error.json").write_text(_json.dumps(
        {"code": "cas_version_mismatch", "message": "expected v2 got v3", "trace_id": "rb"}))

    doc, sup = _make_ctx(tmp_path)
    sup._stage_resolver = lambda manifest: StagePlan.from_single(
        StageSpec(id="gen", label="gen", run=lambda ctx, args: {}))
    assert sorted(sup.recover()) == ["ra", "rb"]

    # 输家 run 详情仍是 error(真相保留);占位快照被台账抑制(核心断言)
    assert next(r for r in sup.snapshot() if r["id"] == "rb")["status"] == "error"
    assert all(s["entity_id"] != "e-1" for s in sup.entity_status_snapshot())
    assert sup.active_entity_ids() == set()   # 两 run 皆终态,不占用圈选


def test_real_failure_after_past_success_stays_visible(tmp_path):
    """code-review 回归:实体曾成功产出,之后一次真失败(非 CAS,如生图 API 错)
    → error 占位必须可见,不被历史成功掩蔽(supersede 只认 cas_version_mismatch)。"""
    doc, sup = _make_ctx(tmp_path)

    # run A:成功 → done 台账记下 e-1
    state_a = sup.start(work_list=_one_item_wl("ra"), item_stage=_stage({}))
    _join(state_a)

    def failing(ctx, args):
        raise RuntimeError("image api down")

    state_b = sup.start(work_list=_one_item_wl("rb"),
                        item_stage=StageSpec(id="gen", label="gen", run=failing))
    _join(state_b)

    snap = [s for s in sup.entity_status_snapshot() if s["entity_id"] == "e-1"]
    assert len(snap) == 1 and snap[0]["phase"] == "error"   # 真失败不掩蔽
    assert snap[0]["error"]["code"] != "cas_version_mismatch"


def test_supervisor_saves_before_appending_oplog(tmp_path):
    """approach B 回归:supervisor 各写路径必须**先 doc.save、再 append OpLog**(与 server/CLI 同序)。

    旧序「先 append 后 save」崩在中间 → OpLog 有、document.json 没存 → reboot 文档落后于 OpLog、
    undo 那些 op 把 reverse 应用到没 forward 的文档 = 数据损坏。播种(entity.add)与提交(entity.update)
    两条路都验:启动审计 op + 两次 add 同批 defer、一次 save 在三次 append 之前;
    收尾 OpLog 记全。"""
    order: list[str] = []
    doc = Document(schema_version="1.0", collaboration_mode="with_save",
                   name="t", schema={"item": ITEM_SCHEMA})
    broker = RealtimeBroker()
    cmd_reg = CommandRegistry(broker=broker)
    register_builtin_commands(cmd_reg)
    oplog = OpLog()
    real_append = oplog.append
    oplog.append = lambda op: order.append("append") or real_append(op)
    sup = PipelineSupervisor(
        doc=doc, cmd_reg=cmd_reg, op_log=oplog, broker=broker,
        data_dir=tmp_path, actor=Actor(user_id="t"), lock=threading.RLock(),
        save_fn=lambda: order.append("save"),
        allow_legacy_test_adapter=True,
    )

    gate = threading.Event()

    def run(ctx, args):
        gate.wait(10)                                    # 阻塞提交,先隔离验播种顺序
        Path(args["_artifacts"][0]).write_text("ok")
        return {"patches": {"title": "done", "done": True}}

    wl = _work_list(run_id="rb", n=2, flaky=-1)          # 两个新实体、无 flaky
    state = sup.start(work_list=wl, item_stage=StageSpec(id="s", label="s", run=run),
                      concurrency=1)

    # start 审计 op + 2 个 entity.add 同一 logical batch：恰一次 save，随后按序 append 三条。
    assert order == ["save", "append", "append", "append"], order

    gate.set()
    _join(state)

    # 收尾:两实体都被 update(done=True),OpLog 记全(1 start + 2 add + 2 update = 5)。
    assert all(e.get("done") is True for e in doc.list("item")), [e.data for e in doc.list("item")]
    assert len(oplog) == 5
    assert order[:4] == ["save", "append", "append", "append"]


def test_supervisor_auto_commit_policy_is_independent_of_collaboration_mode(tmp_path):
    saved: list[str] = []
    doc = Document(
        schema_version="1.0", collaboration_mode="live_collab",
        save_policy="auto_commit", schema={"item": ITEM_SCHEMA},
    )
    supervisor = PipelineSupervisor(
        doc=doc, cmd_reg=CommandRegistry(), op_log=OpLog(), broker=RealtimeBroker(),
        data_dir=tmp_path, actor=Actor(user_id="u"), lock=threading.RLock(),
        save_fn=lambda: saved.append("save"),
        allow_legacy_test_adapter=True,
    )

    supervisor._maybe_save()

    assert saved == ["save"]


def test_supervisor_keeps_pre_w02_with_save_duck_type_compatible(tmp_path):
    """同步到旧下游时，尚无 should_auto_commit 的文档替身不能突然停止保存。"""
    saved: list[str] = []
    doc = SimpleNamespace(collaboration_mode="with_save")
    supervisor = PipelineSupervisor(
        doc=doc, cmd_reg=CommandRegistry(), op_log=OpLog(), broker=RealtimeBroker(),
        data_dir=tmp_path, actor=Actor(user_id="u"), lock=threading.RLock(),
        save_fn=lambda: saved.append("save"),
        allow_legacy_test_adapter=True,
    )

    supervisor._maybe_save()

    assert saved == ["save"]


def test_pipeline_worker_commits_before_publish(tmp_path):
    """A worker commit must publish only after document and operation durability."""
    events: list[str] = []
    doc = Document(schema_version="1.0", collaboration_mode="with_save")
    doc.add(Entity(type="item", data={"x": 1}, id="e1"))
    broker = SimpleNamespace(publish_change=lambda payload: events.append("publish"))
    cmd_reg = CommandRegistry(broker=broker)

    def execute(args, actor, source, doc):
        events.append("mutate")
        ent = doc.get(args["type"], args["id"])
        before = ent.data["x"]
        after = int(args["patches"]["x"])
        ent.update({"x": after}, expected_version=args["expected_version"], actor=actor)
        return new_operation(
            command_id="entity.update", source=source, actor=actor.to_dict(),
            patches=[Patch("item", "e1", "x", "set", before, after)],
            reverse_patches=[Patch("item", "e1", "x", "set", after, before)],
        )

    cmd_reg.register(CommandSpec(id="entity.update", label="update", execute=execute))
    oplog = OpLog()
    real_append = oplog.append
    oplog.append = lambda op: events.append("append") or real_append(op)
    supervisor = PipelineSupervisor(
        doc=doc, cmd_reg=cmd_reg, op_log=oplog, broker=broker,
        data_dir=tmp_path, actor=Actor(user_id="__system__"), lock=threading.RLock(),
        save_fn=lambda: events.append("save"),
        allow_legacy_test_adapter=True,
    )
    state = SimpleNamespace(claimed={"i1": 1}, trace_id="trace")
    item = WorkItem(
        item_id="i1", entity_type="item", entity_id="e1", artifacts=(), payload={},
    )
    import artifact_scan as scan
    attempt = scan.begin_attempt(item, tmp_path, "r", "gen", single_stage=False)

    result = supervisor._commit_stage(state, item, {"patches": {"x": 2}}, attempt)
    events.append("return")

    assert result["ok"] is True
    assert_commit_order("pipeline_worker", events)


def test_auto_retry_skipped_on_cas_conflict_user_edit_wins(tmp_path):
    """2026-07 安全三项③:生成期间用户改了实体 → 写回 CAS 冲突。开着 max_retries 也
    不自动重抢版本重跑(旧行为会让 AI 产出盖掉用户编辑,editor-architecture 规则12)。
    error 占位保留、日志 auto_retry_skipped_cas、文档里用户的值胜出;
    手动 retry_failed(用户明示要 AI 结果)仍可重抢版本覆盖——那是明示意图,不在本测试禁。"""
    doc, sup = _make_ctx(tmp_path)
    wl = _work_list(n=1, flaky=-1)   # 单项,无 flaky

    def run(ctx, args):
        # 模拟用户在生成期间编辑:走 Entity 唯一写通道(CAS 协议),版本随之抬升
        e = doc.find("item", args["_item_id"])
        e.update({"title": "用户改的"}, expected_version=e.version,
                 actor=Actor(user_id="u", source="user"))
        Path(args["_artifacts"][0]).write_text("ai output")
        return {"patches": {"title": "AI盖的", "done": True}}

    state = sup.start(work_list=wl, item_stage=StageSpec(id="gen", label="gen", run=run),
                      max_retries=2)
    _join(state)

    run_info = sup.snapshot()[0]
    assert run_info["status"] == "error"
    item = next(i for i in run_info["active_items"] if i["item_id"] == "g-0")
    assert item["phase"] == "error"
    assert (item.get("error") or {}).get("code") == "cas_version_mismatch"
    # 没有走 auto_retry(旧行为),走了 skip 日志
    assert not any(e.get("code") == "auto_retry" for e in run_info["log_events"])
    assert any(e.get("code") == "auto_retry_skipped_cas" for e in run_info["log_events"])
    # 用户编辑赢:AI 的 patches 没有落库
    ent = doc.find("item", "g-0")
    assert ent.data.get("title") == "用户改的"
    assert not ent.data.get("done")
