"""sdk/test_guided_tour.py — 引导精灵逻辑层单测(系统 3a).

覆盖:实例派生(actor→指路/代操作、confirm 派生自 confirmation_intent、target 投影)、
无实例回退不崩、ad-hoc 规约门禁、摘要 shape。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sdk import (  # noqa: E402
    compile_guided_tours, guided_tour_summaries, find_tour,
    normalize_adhoc_steps, project_semantic_target, TOUR_STEP_KINDS,
)


def _instance() -> dict:
    """最小 tool_understanding 实例(只含 guided_tour 消费的字段)。"""
    return {
        "key_flows": [{
            "id": "flow.demo", "title": "Demo flow",
            "user_goal": "do the thing", "when_to_use": "when you need X",
            "steps": [
                {"id": "s.human_cmd", "actor": "human", "intent": "click add",
                 "command_id": "thing.add",
                 "semantic_target": {"kind": "control", "ref": "control.input"},
                 "confirmation_for_command_ids": [], "confirmation_step_ref": None,
                 "expected_effect": "added"},
                {"id": "s.ai_act", "actor": "ai", "intent": "ai generates",
                 "command_id": "thing.generate",
                 "semantic_target": {"kind": "entity", "ref": "thing"},
                 "confirmation_for_command_ids": [], "confirmation_step_ref": None,
                 "expected_effect": "generated"},
                {"id": "s.confirm", "actor": "human", "intent": "confirm export",
                 "command_id": "thing.export",
                 "semantic_target": {"kind": "control", "ref": "control.result"},
                 "confirmation_for_command_ids": ["thing.export"],
                 "confirmation_step_ref": None, "expected_effect": "exported"},
                {"id": "s.look", "actor": "human", "intent": "review result",
                 "command_id": None,
                 "semantic_target": {"kind": "entity", "ref": "thing"},
                 "confirmation_for_command_ids": [], "confirmation_step_ref": None,
                 "expected_effect": ""},
            ],
        }],
        "command_semantics": [
            {"command_id": "thing.add", "confirmation_intent": "none"},
            {"command_id": "thing.generate", "confirmation_intent": "none"},
            {"command_id": "thing.export", "confirmation_intent": "human_required"},
        ],
    }


def test_instance_projection_maps_actor_and_derives_confirm():
    tours = compile_guided_tours(tool_understanding=_instance())
    assert len(tours) == 1
    tour = tours[0]
    assert tour["source"] == "instance"
    assert tour["id"] == "flow.demo"
    assert tour["trigger"] == "do the thing"       # user_goal
    assert tour["when"] == "when you need X"        # when_to_use
    by_id = {s["id"]: s for s in tour["steps"]}

    # human + command → 指路(point / guide mode),不替用户执行。
    human = by_id["s.human_cmd"]
    assert human["kind"] == "point" and human["mode"] == "guide"
    assert human["command_id"] == "thing.add" and human["confirm"] is False
    assert human["target"] == "control:control.input"

    # ai + command(非高危)→ 代操作(act mode);confirm 派生自 confirmation_intent。
    ai = by_id["s.ai_act"]
    assert ai["kind"] == "act" and ai["mode"] == "act"
    assert ai["confirm"] is False and ai["target"] == "entity:thing"

    # human 确认提供步 → ask;其命令 human_required → confirm 派生为 True。
    confirm = by_id["s.confirm"]
    assert confirm["kind"] == "ask" and confirm["confirm"] is True

    # human 无命令 + entity 目标 → highlight(看结果)。
    look = by_id["s.look"]
    assert look["kind"] == "highlight" and look["mode"] == "guide"

    # 全步 kind 落在封闭集内;tour 含 act + 非 act → mixed。
    assert all(s["kind"] in TOUR_STEP_KINDS for s in tour["steps"])
    assert tour["mode"] == "mixed"


def test_confirm_is_derived_not_authored():
    """把 confirmation_intent 改成 none,export 步的 confirm 必须随之变 False。"""
    record = _instance()
    for c in record["command_semantics"]:
        if c["command_id"] == "thing.export":
            c["confirmation_intent"] = "none"
    tour = compile_guided_tours(tool_understanding=record)[0]
    confirm = {s["id"]: s for s in tour["steps"]}["s.confirm"]
    assert confirm["confirm"] is False


def test_summaries_shape():
    tours = compile_guided_tours(tool_understanding=_instance())
    summaries = guided_tour_summaries(tours)
    assert summaries and set(summaries[0]) == {
        "id", "title", "trigger", "when", "mode", "step_count"
    }
    assert summaries[0]["step_count"] == 4


def test_fallback_from_ai_flow_does_not_crash():
    tours = compile_guided_tours(
        tool_understanding=None,
        ai_flow={"summary": "S", "steps": ["step a", "step b"]},
    )
    assert len(tours) == 1
    tour = tours[0]
    assert tour["source"] == "fallback" and tour["mode"] == "guide"
    assert [s["kind"] for s in tour["steps"]] == ["say", "say"]
    assert tour["trigger"] == "S"


def test_fallback_from_manual_only():
    tours = compile_guided_tours(
        tool_understanding=None, ai_flow={}, manual="# Title\n\nBody text.",
    )
    assert len(tours) == 1 and tours[0]["steps"][0]["kind"] == "say"


def test_fallback_empty_yields_no_tours():
    assert compile_guided_tours() == []
    assert compile_guided_tours(tool_understanding={}, ai_flow={}, manual="") == []


def test_no_key_flows_instance_falls_back():
    tours = compile_guided_tours(
        tool_understanding={"command_semantics": []},
        ai_flow={"summary": "S", "steps": ["a"]},
    )
    assert tours and tours[0]["source"] == "fallback"


def test_project_semantic_target_forms():
    assert project_semantic_target({"kind": "command", "ref": "x.y"}) == "cmd:x.y"
    assert project_semantic_target({"kind": "tool", "ref": "t"}) == "tool:t"
    assert project_semantic_target({"kind": "bogus", "ref": "z"}) == ""
    assert project_semantic_target(None) == ""


def test_find_tour():
    tours = compile_guided_tours(tool_understanding=_instance())
    assert find_tour(tours, "flow.demo")["id"] == "flow.demo"
    assert find_tour(tours, "missing") is None


def test_normalize_adhoc_rejects_bad_kind_and_missing_command():
    with pytest.raises(ValueError):
        normalize_adhoc_steps([{"kind": "frobnicate"}])
    with pytest.raises(ValueError):
        normalize_adhoc_steps([{"kind": "act"}])          # act 缺 command_id


def test_normalize_adhoc_derives_confirm_from_registry():
    steps = normalize_adhoc_steps(
        [{"kind": "act", "command_id": "danger.op", "args": {"n": 1}}],
        confirm_lookup={"danger.op": True},
    )
    assert steps[0]["kind"] == "act" and steps[0]["confirm"] is True
    assert steps[0]["args"] == {"n": 1}
    # act 步默认 actor=ai(代操作)。
    assert steps[0]["actor"] == "ai" and steps[0]["mode"] == "act"
