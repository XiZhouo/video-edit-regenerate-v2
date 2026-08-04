"""test_stage_plan — 多 stage 数据模型(P2a)。

覆盖:
  - StagePlan 校验(空 → 报错 / 重复 id → 报错)
  - from_single 桥接(单 stage 回退)
  - index_of / stage_ids / is_single / len / getitem
  - to_meta 可序列化身份(不含 run callable)
  - StageSpec 新增 produces/resource_scopes 默认空(单 stage 旧行为不变)
  - artifact_scan stage 标记:连续前缀语义 / 续跑安全 / done 判定 / 清标记
"""
from __future__ import annotations

import pytest

import artifact_scan as scan
from stage import StageSpec
from stage_plan import StagePlan
from work_list import WorkItem, WorkList


def _stage(sid: str, **kw) -> StageSpec:
    return StageSpec(id=sid, label=sid.upper(), run=lambda ctx, args: {}, **kw)


# ---- StageSpec 加性字段 ----

def test_stagespec_new_fields_default_empty():
    s = _stage("a")
    assert s.produces == ()
    assert s.resource_scopes == ()
    assert s.resource_costs == {}
    assert s.command_id == ""
    assert s.input_ports == ()
    assert s.output_ports == ()
    assert s.interrupt_safe is False
    assert s.pause_after is False
    s2 = _stage("b", produces=("x.png",), resource_scopes=("gpu",),
                resource_costs={"gpu": 4},
                command_id="cmd.b", input_ports=({"id": "in"},),
                output_ports=({"id": "out"},), interrupt_safe=True,
                pause_after=True)
    assert s2.produces == ("x.png",)
    assert s2.resource_scopes == ("gpu",)
    assert s2.resource_costs == {"gpu": 4}
    assert s2.command_id == "cmd.b"
    assert s2.input_ports == ({"id": "in"},)
    assert s2.output_ports == ({"id": "out"},)
    assert s2.interrupt_safe is True
    assert s2.pause_after is True


# ---- StagePlan 校验 ----

def test_stageplan_rejects_empty():
    with pytest.raises(ValueError):
        StagePlan(())


def test_stageplan_rejects_dup_ids():
    with pytest.raises(ValueError):
        StagePlan((_stage("a"), _stage("a")))


def test_from_single_wraps_one_stage():
    p = StagePlan.from_single(_stage("only"))
    assert len(p) == 1
    assert p.is_single
    assert p.stage_ids == ("only",)
    assert p[0].id == "only"


def test_index_of_and_queries():
    p = StagePlan((_stage("gen"), _stage("post"), _stage("pack")))
    assert not p.is_single
    assert len(p) == 3
    assert p.stage_ids == ("gen", "post", "pack")
    assert p.index_of("post") == 1
    with pytest.raises(ValueError):
        p.index_of("nope")


def test_to_meta_is_serializable_without_run():
    p = StagePlan((_stage("gen", produces=("a.png",), resource_scopes=("gpu",),
                          resource_costs={"gpu": 4},
                          command_id="cmd.gen", input_ports=({"id": "src"},),
                          output_ports=({"id": "out"},), interrupt_safe=True,
                          pause_after=True),))
    meta = p.to_meta()
    assert meta == [{
        "id": "gen",
        "label": "GEN",
        "command_id": "cmd.gen",
        "produces": ["a.png"],
        "resource_scopes": ["gpu"],
        "resource_costs": {"gpu": 4},
        "retry_safe": True,
        "interrupt_safe": True,
        "pause_after": True,
        "input_ports": [{"id": "src"}],
        "output_ports": [{"id": "out"}],
    }]
    import json
    json.dumps(meta)  # 不抛 = 可序列化


def test_to_meta_includes_backward_compatible_authority_defaults():
    meta = StagePlan.from_single(_stage("legacy")).to_meta()
    assert meta[0]["resource_costs"] == {}
    assert meta[0]["pause_after"] is False


def test_to_meta_detaches_resource_costs_from_stage_spec():
    costs = {"gpu": 2}
    plan = StagePlan.from_single(_stage(
        "gen", resource_scopes=("gpu",), resource_costs=costs,
    ))

    meta = plan.to_meta()
    meta[0]["resource_costs"]["gpu"] = 99

    assert costs == {"gpu": 2}
    assert plan[0].resource_costs == {"gpu": 2}


@pytest.mark.parametrize("field,value", (
    ("retry_safe", 1),
    ("interrupt_safe", 0),
    ("pause_after", "false"),
))
def test_stagespec_rejects_truthy_falsy_boolean_normalization(field, value):
    with pytest.raises(TypeError, match="精确 bool"):
        _stage("strict-bool", **{field: value})


@pytest.mark.parametrize("field,value", (
    ("produces", ["a.png"]),
    ("resource_scopes", ["gpu"]),
    ("input_ports", [{"id": "in"}]),
    ("output_ports", [{"id": "out"}]),
))
def test_stagespec_requires_exact_tuple_carriers(field, value):
    with pytest.raises(TypeError, match="精确 tuple"):
        _stage("strict-tuple", **{field: value})


def test_stageplan_owns_stage_intent_and_nested_port_extension_bags():
    from dataclasses import FrozenInstanceError

    costs = {"gpu": 2}
    flags = [{"enabled": True}]
    port = {
        "id": "video",
        "kind": "payload",
        "extension": {"timeline": {"flags": flags}},
    }
    source = _stage(
        "generate",
        resource_scopes=("gpu",),
        resource_costs=costs,
        retry_safe=True,
        output_ports=(port,),
    )
    plan = StagePlan.from_single(source)

    source.__dict__["retry_safe"] = False
    plan.__dict__["stages"] = ()
    assert source.retry_safe is True
    assert plan.stage_ids == ("generate",)
    assert plan[0] is not source
    assert plan[0].run is source.run
    costs["gpu"] = 99
    flags[0]["enabled"] = False
    port["id"] = "caller-mutated"

    with pytest.raises(FrozenInstanceError):
        source.retry_safe = False
    # Even a deliberate dataclass bypass on the caller object cannot rewrite the plan clone.
    object.__setattr__(source, "retry_safe", False)
    object.__setattr__(source, "id", "caller-rebound")

    meta = plan.to_meta()
    assert meta[0]["id"] == "generate"
    assert meta[0]["retry_safe"] is True
    assert meta[0]["resource_costs"] == {"gpu": 2}
    assert meta[0]["output_ports"] == [{
        "id": "video",
        "kind": "payload",
        "extension": {"timeline": {"flags": [{"enabled": True}]}},
    }]

    # Public reads preserve the legacy plain-JSON shape, but every projection is detached.
    public_costs = plan[0].resource_costs
    public_ports = plan[0].output_ports
    dict.__setitem__(public_costs, "gpu", 7)
    dict.__setitem__(public_ports[0], "id", "forged")
    list.__setitem__(
        public_ports[0]["extension"]["timeline"]["flags"], 0, {},
    )
    assert plan[0].resource_costs == {"gpu": 2}
    assert plan[0].output_ports[0]["id"] == "video"
    assert plan[0].output_ports[0]["extension"]["timeline"]["flags"] == [
        {"enabled": True}
    ]

    # Every serialization is detached all the way down.
    meta[0]["resource_costs"]["gpu"] = 123
    meta[0]["output_ports"][0]["extension"]["timeline"]["flags"][0][
        "enabled"
    ] = False
    assert plan.to_meta()[0]["resource_costs"] == {"gpu": 2}
    assert plan.to_meta()[0]["output_ports"][0]["extension"]["timeline"][
        "flags"
    ][0]["enabled"] is True


def test_stagespec_rejects_non_json_or_cyclic_port_graphs():
    cyclic: dict[str, object] = {"id": "out"}
    cyclic["loop"] = cyclic
    with pytest.raises(ValueError, match="容器循环"):
        _stage("cycle", output_ports=(cyclic,))
    with pytest.raises(TypeError, match="精确 JSON"):
        _stage("object", output_ports=({"id": "out", "value": object()},))


# ---- artifact_scan stage 标记 ----

def _item() -> WorkItem:
    return WorkItem(item_id="i1", entity_type="t", entity_id="e1", artifacts=())


def test_stage_markers_prefix_and_resume(tmp_path):
    plan = StagePlan((_stage("a"), _stage("b"), _stage("c")))
    it = _item()
    rid = "r1"
    assert scan.completed_stage_count(it, tmp_path, rid, plan) == 0
    scan.mark_stage_done(tmp_path, rid, it.item_id, "a")
    assert scan.completed_stage_count(it, tmp_path, rid, plan) == 1
    # 跳着完成 c 但 b 缺 → 前缀仍停在 1(续跑从 b 接,不误判 done)
    scan.mark_stage_done(tmp_path, rid, it.item_id, "c")
    assert scan.completed_stage_count(it, tmp_path, rid, plan) == 1
    assert not scan.stage_item_done(it, tmp_path, rid, plan)
    scan.mark_stage_done(tmp_path, rid, it.item_id, "b")
    assert scan.completed_stage_count(it, tmp_path, rid, plan) == 3
    assert scan.stage_item_done(it, tmp_path, rid, plan)


def test_scan_item_staged_phases(tmp_path):
    plan = StagePlan((_stage("a"), _stage("b")))
    it = _item()
    rid = "r2"
    assert scan.scan_item_staged(it, tmp_path, rid, plan) == "pending"
    scan.mark_stage_done(tmp_path, rid, it.item_id, "a")
    scan.mark_stage_done(tmp_path, rid, it.item_id, "b")
    assert scan.scan_item_staged(it, tmp_path, rid, plan) == "done"
    # error 标记优先于 done
    em = scan.error_marker_path(tmp_path, rid, it.item_id)
    em.parent.mkdir(parents=True, exist_ok=True)
    em.write_text("{}")
    assert scan.scan_item_staged(it, tmp_path, rid, plan) == "error"


def test_item_id_rejects_path_traversal():
    # item_id 进标记文件名 → 拒绝分隔符 / null,防逃出 status/ 目录
    for bad in ("../x", "a/b", "a\\b", "x\x00y"):
        with pytest.raises(ValueError):
            WorkItem(item_id=bad, entity_type="t", entity_id="e")


@pytest.mark.parametrize("bad", ("", ".", "..", "a/b", "a\\b", "x\x00y"))
def test_run_item_and_stage_ids_are_safe_nonempty_components(bad):
    from work_list import WorkList
    with pytest.raises(ValueError):
        WorkList(run_id=bad)
    with pytest.raises(ValueError):
        WorkItem(item_id=bad, entity_type="t", entity_id="e")
    with pytest.raises(ValueError):
        StageSpec(id=bad, label="bad", run=lambda _c, _a: {})


@pytest.mark.parametrize("bad", ("", ".", "../x", "/abs", "C:/abs", "a\\b", "x\x00y"))
def test_artifact_paths_reject_ambiguous_or_non_posix_targets(bad):
    with pytest.raises(ValueError):
        WorkItem(item_id="i", entity_type="t", entity_id="e", artifacts=(bad,))


def test_artifact_paths_must_be_unique_after_normalization():
    with pytest.raises(ValueError):
        WorkItem(
            item_id="i", entity_type="t", entity_id="e",
            artifacts=("out/a.txt", "out//a.txt"),
        )


@pytest.mark.parametrize("payload", (
    {"seed_data": {"source": "a"}, "recovery_identity_fields": []},
    {"seed_data": {"source": "a"}, "recovery_identity_fields": ["source", "source"]},
    {"recovery_identity_fields": ["source"]},
    {"seed_data": {"source": "a"}, "recovery_identity_fields": ["missing"]},
    {"seed_data": {"source": "a"}, "recovery_identity_fields": "source"},
))
def test_recovery_identity_declaration_is_nonempty_unique_and_seed_bound(payload):
    with pytest.raises(ValueError, match="recovery_identity_fields"):
        WorkItem(item_id="i", entity_type="t", entity_id="e", payload=payload)


def test_recovery_identity_declaration_accepts_named_seed_members():
    item = WorkItem(
        item_id="i", entity_type="t", entity_id="e",
        payload={
            "seed_data": {"source": {"sha256": "a" * 64}, "adapter": "mock"},
            "recovery_identity_fields": ["source", "adapter"],
        },
    )
    assert item.payload["recovery_identity_fields"] == ["source", "adapter"]


def test_work_item_deeply_owns_and_freezes_its_payload_snapshot():
    import pickle

    source = {
        "seed_data": {"source": {"sha256": "a" * 64}},
        "source": {"sha256": "a" * 64},
        "recovery_identity_fields": ["source"],
    }
    item = WorkItem(item_id="i", entity_type="t", entity_id="e", payload=source)

    source["seed_data"]["source"]["sha256"] = "b" * 64
    source["recovery_identity_fields"].clear()
    with pytest.raises(TypeError, match="immutable owned snapshot"):
        item.payload["recovery_identity_fields"].clear()

    detached = item.to_dict()["payload"]
    assert detached["seed_data"]["source"]["sha256"] == "a" * 64
    assert detached["recovery_identity_fields"] == ["source"]
    detached["recovery_identity_fields"].clear()
    detached["seed_data"]["source"]["sha256"] = "c" * 64
    assert item.payload_copy()["recovery_identity_fields"] == ["source"]
    assert item.payload_copy()["seed_data"]["source"]["sha256"] == "a" * 64
    restored = pickle.loads(pickle.dumps(item))
    assert restored.to_dict() == item.to_dict()
    assert type(item._payload_snapshot) is bytes
    with pytest.raises(TypeError):
        dict.__setitem__(item._payload_snapshot, "forged", True)
    with pytest.raises(TypeError, match="immutable owned snapshot"):
        restored.payload["seed_data"].clear()


def test_work_item_rejects_non_json_without_invoking_copy_hooks():
    class CopyAlias:
        called = False

        def __deepcopy__(self, _memo):
            type(self).called = True
            return self

    with pytest.raises(ValueError, match="deeply ownable"):
        WorkItem(
            item_id="i",
            entity_type="item",
            entity_id="e",
            payload={"forged": CopyAlias()},
        )
    assert CopyAlias.called is False


@pytest.mark.parametrize(
    "field,value",
    (
        ("payload", [["x", 1]]),
        ("payload", []),
        ("artifacts", "a"),
        ("artifacts", {}),
    ),
)
def test_work_item_manifest_rejects_lossy_container_coercion(field, value):
    manifest = {
        "item_id": "i",
        "entity_type": "item",
        "entity_id": "e",
        "artifacts": [],
        "payload": {},
    }
    manifest[field] = value
    with pytest.raises(TypeError):
        WorkItem.from_dict(manifest)


def test_work_list_manifest_requires_json_list_and_exact_item_objects():
    item = {
        "item_id": "i",
        "entity_type": "item",
        "entity_id": "e",
        "artifacts": [],
        "payload": {},
    }
    with pytest.raises(TypeError):
        WorkList.from_dict({"run_id": "r", "items": (item,)})
    with pytest.raises(TypeError):
        WorkList.from_dict({"run_id": "r", "items": [list(item.items())]})

    # Missing optional JSON members remain the one intentional legacy normalization.
    assert WorkItem.from_dict({
        "item_id": "i", "entity_type": "item", "entity_id": "e",
    }).to_dict()["payload"] == {}


@pytest.mark.parametrize("duplicate", ("item", "target", "artifact"))
def test_work_list_rejects_cross_item_identity_collisions(duplicate):
    first = WorkItem(
        item_id="a", entity_type="item", entity_id="e1", artifacts=("out/a.txt",),
    )
    second = WorkItem(
        item_id="a" if duplicate == "item" else "b",
        entity_type="item",
        entity_id="e1" if duplicate == "target" else "e2",
        artifacts=("out/a.txt" if duplicate == "artifact" else "out/b.txt",),
    )
    with pytest.raises(ValueError, match="跨 item 唯一"):
        WorkList(run_id="r1", items=[first, second])


def test_work_list_owns_order_items_and_hidden_manifest_snapshot():
    from dataclasses import FrozenInstanceError

    first = WorkItem(
        item_id="a", entity_type="item", entity_id="e1",
        payload={"nested": ["original"]},
    )
    second = WorkItem(item_id="b", entity_type="item", entity_id="e2")
    caller_items = [first, second]
    work_list = WorkList(run_id="owned", name="Owned", items=caller_items)

    first.__dict__["item_id"] = "forged"
    work_list.__dict__["run_id"] = "forged"
    assert first.item_id == "a"
    assert work_list.run_id == "owned"
    assert isinstance(work_list.items, tuple)
    assert work_list.items[0] is not first
    caller_items.reverse()
    caller_items.append(WorkItem(item_id="c", entity_type="item", entity_id="e3"))
    assert [item.item_id for item in work_list.items] == ["a", "b"]

    with pytest.raises(AttributeError):
        work_list.items.append(first)
    with pytest.raises(FrozenInstanceError):
        work_list.items = ()
    with pytest.raises(FrozenInstanceError):
        work_list.run_id = "rebound"

    # WorkItem keeps its own hidden payload snapshot, so bypassing the read-view's overridden
    # mutator still cannot alter execution args or WorkList's independently owned manifest.
    list.__setitem__(work_list.items[0].payload["nested"], 0, "forged")
    assert work_list.items[0].payload_copy() == {"nested": ["original"]}
    assert work_list.to_dict() == {
        "run_id": "owned",
        "name": "Owned",
        "items": [
            {
                "item_id": "a", "entity_type": "item", "entity_id": "e1",
                "artifacts": [], "payload": {"nested": ["original"]},
            },
            {
                "item_id": "b", "entity_type": "item", "entity_id": "e2",
                "artifacts": [], "payload": {},
            },
        ],
    }

    detached = work_list.to_dict()
    detached["run_id"] = "forged"
    detached["items"].reverse()
    detached["items"][0]["payload"]["new"] = True
    assert work_list.to_dict()["run_id"] == "owned"
    assert [item["item_id"] for item in work_list.to_dict()["items"]] == ["a", "b"]


def test_work_list_execution_and_manifest_share_canonical_identity_snapshots():
    work_list = WorkList(run_id="owned", name="Owned", items=[WorkItem(
        item_id="a",
        entity_type="item",
        entity_id="e1",
        payload={"nested": ["original"]},
    )])

    # Deliberate public-field reflection writes cannot make Supervisor-facing reads disagree
    # with the manifest projection.  Both continue to consume the construction-time authority.
    object.__setattr__(work_list.items[0], "entity_id", "forged-entity")
    object.__setattr__(work_list.items[0], "payload", {"nested": ["forged"]})
    object.__setattr__(work_list, "run_id", "forged-run")
    object.__setattr__(work_list, "items", ())

    assert work_list.run_id == "owned"
    assert len(work_list.items) == 1
    assert work_list.items[0].entity_id == "e1"
    assert work_list.items[0].payload == {"nested": ["original"]}
    assert work_list.items[0].payload_copy() == {"nested": ["original"]}
    assert work_list.to_dict()["items"][0]["entity_id"] == "e1"
    assert work_list.to_dict()["items"][0]["payload"] == {"nested": ["original"]}

    old_view = work_list.items[0].payload
    list.__setitem__(old_view["nested"], 0, "view-only-forgery")
    assert work_list.items[0].payload == {"nested": ["original"]}


def test_stageplan_and_worklist_snapshots_are_stable_under_concurrent_reads():
    import concurrent.futures
    import json

    plan = StagePlan.from_single(_stage(
        "concurrent",
        resource_scopes=("gpu",),
        resource_costs={"gpu": 3},
        retry_safe=False,
        output_ports=({"id": "out", "extension": {"flags": [1, 2, 3]}},),
    ))
    work_list = WorkList(run_id="concurrent", items=[WorkItem(
        item_id="i", entity_type="item", entity_id="e",
        payload={"nested": {"values": [1, 2, 3]}},
    )])

    expected_meta = json.dumps(plan.to_meta(), sort_keys=True)
    expected_work = json.dumps(work_list.to_dict(), sort_keys=True)

    def read_many(_index: int) -> tuple[str, str]:
        for _ in range(100):
            meta = plan.to_meta()
            manifest = work_list.to_dict()
            meta[0]["output_ports"][0]["extension"]["flags"].reverse()
            manifest["items"][0]["payload"]["nested"]["values"].reverse()
        return (
            json.dumps(plan.to_meta(), sort_keys=True),
            json.dumps(work_list.to_dict(), sort_keys=True),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read_many, range(24)))
    assert set(results) == {(expected_meta, expected_work)}


def test_supervisor_stage_ports_are_linear_deep_plain_json_projections():
    from supervisor import PipelineSupervisor

    stage = _stage("ports", output_ports=({
        "id": "out",
        "extension": {"flags": [{"enabled": True}]},
        **{f"key-{index}": index for index in range(1_000)},
    },))
    supervisor = object.__new__(PipelineSupervisor)

    ports = supervisor._stage_ports(stage, "output")

    assert type(ports[0]) is dict
    assert type(ports[0]["extension"]) is dict
    assert type(ports[0]["extension"]["flags"]) is list
    assert type(ports[0]["extension"]["flags"][0]) is dict
    ports[0]["extension"]["flags"][0]["enabled"] = False
    assert stage.output_ports[0]["extension"]["flags"][0]["enabled"] is True


def test_immutable_intent_deepcopy_rebuilds_valid_detached_snapshots():
    import copy

    plan = StagePlan.from_single(_stage(
        "copyable",
        output_ports=({"id": "out", "extension": {"flags": [1]}},),
    ))
    copied = copy.deepcopy(plan)

    assert copied is not plan
    assert copied[0] is not plan[0]
    assert copied[0].run is plan[0].run
    carrier = copied.to_meta()
    carrier[0]["output_ports"][0]["extension"]["flags"].append(2)
    assert plan.to_meta()[0]["output_ports"][0]["extension"]["flags"] == [1]


def test_frozen_intent_keeps_dataclass_replace_and_json_read_compatibility():
    import json
    from dataclasses import replace

    stage = _stage(
        "replaceable",
        resource_scopes=("gpu",),
        resource_costs={"gpu": 2},
        output_ports=({"id": "out", "extension": {"flags": [1]}},),
    )
    replaced_stage = replace(stage, label="Replaced")
    assert replaced_stage.label == "Replaced"
    assert replaced_stage.resource_costs == {"gpu": 2}
    assert json.loads(json.dumps(replaced_stage.resource_costs)) == {"gpu": 2}
    assert json.loads(json.dumps(replaced_stage.output_ports))[0]["id"] == "out"

    item = WorkItem(
        item_id="i", entity_type="item", entity_id="e", payload={"value": [1]},
    )
    replaced_item = replace(item, entity_id="e2")
    assert replaced_item.entity_id == "e2"
    assert replaced_item.payload_copy() == {"value": [1]}


def test_stage_id_rejects_path_traversal():
    with pytest.raises(ValueError):
        StagePlan((_stage("a/b"),))
    with pytest.raises(ValueError):
        StagePlan((_stage("x\x00y"),))


def test_eta_throughput():
    # 样本不足 → (None, None)
    assert scan.eta_throughput(0, 5, 10.0) == (None, None)
    assert scan.eta_throughput(2, 5, 0.0) == (None, None)
    # 2 项/10 秒 → 0.2 item/s;剩 8 项 → 40 秒
    eta, tp = scan.eta_throughput(2, 8, 10.0)
    assert tp == 0.2 and eta == 40.0
    # 已跑完(pending=0)→ eta=0
    eta2, tp2 = scan.eta_throughput(10, 0, 5.0)
    assert eta2 == 0.0 and tp2 == 2.0


def test_stage_summary(tmp_path):
    plan = StagePlan((_stage("a"), _stage("b"), _stage("c")))
    items = [WorkItem(item_id=f"i{i}", entity_type="t", entity_id=f"e{i}") for i in range(3)]
    wl = WorkList(run_id="rs", items=items)
    # i0: a,b done;i1: a done;i2: 无
    scan.mark_stage_done(tmp_path, "rs", "i0", "a")
    scan.mark_stage_done(tmp_path, "rs", "i0", "b")
    scan.mark_stage_done(tmp_path, "rs", "i1", "a")
    # i2: 只标 b、缺 a(前缀有缺口)→ completed=0,不计入任何 stage 的 done
    scan.mark_stage_done(tmp_path, "rs", "i2", "b")
    summ = scan.stage_summary(wl, tmp_path, plan)
    assert summ == [
        {"stage_id": "a", "label": "A", "done": 2, "total": 3},
        {"stage_id": "b", "label": "B", "done": 1, "total": 3},   # i2 的孤立 b 不算
        {"stage_id": "c", "label": "C", "done": 0, "total": 3},
    ]


def test_clear_stage_markers(tmp_path):
    plan = StagePlan((_stage("a"), _stage("b")))
    it = _item()
    rid = "r3"
    scan.mark_stage_done(tmp_path, rid, it.item_id, "a")
    scan.mark_stage_done(tmp_path, rid, it.item_id, "b")
    assert scan.completed_stage_count(it, tmp_path, rid, plan) == 2
    scan.clear_stage_markers(tmp_path, rid, it.item_id, plan)
    assert scan.completed_stage_count(it, tmp_path, rid, plan) == 0
