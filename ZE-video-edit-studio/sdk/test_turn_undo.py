"""§7.4 P0-D 补偿规划器测试(Form B):op patches → 补偿正向命令 + guard,或跳过并列出。"""

from __future__ import annotations

from sdk.turn_undo import plan_turn_compensation, _classify_op


def _op(op_id, patches):
    return {"id": op_id, "patches": patches}


def _set_patch(et, eid, field, before, after):
    return {"entity_type": et, "entity_id": eid, "field": field, "op": "set",
            "before": before, "after": after}


# ── entity.update 补偿(设回 before + guard current==after)──────────────────────

def test_update_op_compensates_to_before_with_guards():
    op = _op("a" * 32, [
        _set_patch("box", "b1", "title", before="旧", after="新"),
        _set_patch("box", "b1", "color", before="#000", after="#fff"),
    ])
    item = _classify_op(op)
    assert item["action"] == "compensate"
    assert item["command_id"] == "entity.update"
    assert item["args"] == {"type": "box", "id": "b1", "patches": {"title": "旧", "color": "#000"}}
    # guard:每字段校 current==after(未被后续覆盖才补偿)
    guards = {(g["field"], g["expect_after"]) for g in item["guards"]}
    assert guards == {("title", "新"), ("color", "#fff")}


def test_name_patch_compensates_via_name_arg():
    op = _op("a" * 32, [{"entity_type": "t", "entity_id": "e", "field": "name",
                         "op": "name", "before": "旧名", "after": "新名"}])
    item = _classify_op(op)
    assert item["command_id"] == "entity.update"
    assert item["args"]["name"] == "旧名"
    assert item["guards"][0] == {"kind": "field_unchanged", "entity_type": "t",
                                 "entity_id": "e", "field": "name", "expect_after": "新名"}


# ── entity.add 补偿(→ delete,guard 实体仍在)────────────────────────────────────

def test_create_op_compensates_with_delete():
    op = _op("a" * 32, [{"entity_type": "box", "entity_id": "b9", "field": "__entity__",
                         "op": "create", "before": None, "after": {"id": "b9"}}])
    item = _classify_op(op)
    assert item["action"] == "compensate"
    assert item["command_id"] == "entity.delete"
    assert item["args"] == {"type": "box", "id": "b9"}
    assert item["guards"] == [{"kind": "entity_exists", "entity_type": "box", "entity_id": "b9"}]


# ── 跳过并列出(§7.4 明确列出不能撤销的部分)──────────────────────────────────

def test_delete_lifecycle_op_is_skipped():
    op = _op("a" * 32, [{"entity_type": "box", "entity_id": "b1", "field": "__entity__",
                         "op": "delete", "before": {"full": "snapshot"}, "after": None}])
    item = _classify_op(op)
    assert item["action"] == "skip"
    assert "删除" in item["skip_reason"] or "恢复" in item["skip_reason"]


def test_ancestry_or_dotpath_patch_is_skipped():
    op = _op("a" * 32, [{"entity_type": "t", "entity_id": "e", "field": "box.a", "op": "set",
                         "before": 1, "after": 2}])
    assert _classify_op(op)["action"] == "skip"
    op2 = _op("b" * 32, [{"entity_type": "t", "entity_id": "e", "field": "f", "op": "set",
                          "before": 1, "after": 2, "ancestry": {"field": "root"}}])
    assert _classify_op(op2)["action"] == "skip"


def test_multi_entity_op_is_skipped():
    op = _op("a" * 32, [
        _set_patch("t", "e1", "f", 1, 2),
        _set_patch("t", "e2", "g", 3, 4),
    ])
    item = _classify_op(op)
    assert item["action"] == "skip"
    assert "跨" in item["skip_reason"]


def test_empty_patches_op_is_skipped():
    assert _classify_op(_op("a" * 32, []))["action"] == "skip"


# ── plan_turn_compensation 聚合(compensable / skipped 分列)──────────────────────

def test_plan_aggregates_compensable_and_skipped_in_order():
    ops = [
        _op("u" * 32, [_set_patch("box", "b1", "title", "旧", "新")]),          # 可补偿
        _op("d" * 32, [{"entity_type": "box", "entity_id": "b2", "field": "__entity__",
                        "op": "delete", "before": {}, "after": None}]),          # 跳过(删除)
        _op("c" * 32, [{"entity_type": "box", "entity_id": "b3", "field": "__entity__",
                        "op": "create", "before": None, "after": {}}]),          # 可补偿(→delete)
    ]
    out = plan_turn_compensation(ops)
    assert out["compensable"] == ["u" * 32, "c" * 32]
    assert [s["op_id"] for s in out["skipped"]] == ["d" * 32]
    assert [i["op_id"] for i in out["plan"]] == ["u" * 32, "d" * 32, "c" * 32]   # 保新→旧序
