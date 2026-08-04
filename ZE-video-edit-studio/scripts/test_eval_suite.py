"""eval_suite 单测:声明式 command benchmark 能校验结果、错误与 partial 断言。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_suite as ev  # noqa: E402


def test_contains_subset_allows_partial_nested_dicts():
    actual = {"a": 1, "b": {"x": 2, "y": 3}, "c": [1, {"z": 4}]}
    assert ev.contains_subset(actual, {"b": {"x": 2}})
    assert ev.contains_subset(actual, {"c": [1, {"z": 4}]})
    assert not ev.contains_subset(actual, {"b": {"x": 9}})


def test_current_tree_eval_cases_pass():
    res = ev.run()
    assert res["ok"], res
    # 下游 scaffold skill 常态:未声明 registry.eval_cases → run() 返回 {"skipped": ...} 且无 case_count → 跳过。
    # 原断言 case_count>=3 / "item.toggle_done" 写死了 build-v3 自带 demo 的命令名,任何下游 skill 必假失败、
    # 挡住 verify_all 出 evidence(门禁本体 eval_suite.py 已 res.get('case_count',0) 容错,唯本自测漏了)。
    if res.get("skipped"):
        import pytest
        pytest.skip("current tree declares no registry.eval_cases")
    assert res["case_count"] >= 1
    assert isinstance(res.get("covered_commands"), list)


class _StubPending:
    """与 sdk.PendingConfirmation 同名同形的替身(单测 _run_case 分支逻辑,不碰真注册表)。"""

    def __init__(self, token="tok-1", command_id="danger.zone"):
        self.token = token
        self.command_id = command_id


def _stub_repo(dispatch_result):
    class _Doc:
        def add(self, e): pass
        def get(self, t, i): raise KeyError(i)
        def list(self, t): return []
    class _Reg:
        def dispatch(self, *a, **k):
            if isinstance(dispatch_result, Exception):
                raise dispatch_result
            return dispatch_result
    class _Ctx:
        doc = _Doc(); cmd_reg = _Reg(); op_log = None
    class _Repo:
        def create_project(self, **k): return "p1"
        def get_or_load(self, pid): return _Ctx()
    return _Repo()


def test_pending_confirmation_case_passes_on_gate(monkeypatch):
    """C5:requires_confirm 一阶段用例——dispatch 返回 PendingConfirmation 即 pass。"""
    import sdk
    monkeypatch.setattr(sdk, "PendingConfirmation", _StubPending, raising=True)
    case = {"id": "gates", "command": "danger.zone",
            "expect": {"status": "pending_confirmation"}}
    res = ev._run_case(_stub_repo(_StubPending()), case)
    assert res["status"] == "passed", res


def test_pending_confirmation_case_fails_when_executed(monkeypatch):
    """C5 反例:期望 gate 却直接执行(返回带 to_dict 的 op)→ 必须 fail。"""
    import sdk
    monkeypatch.setattr(sdk, "PendingConfirmation", _StubPending, raising=True)
    class _Op:
        def to_dict(self): return {"kind": "native", "patches": []}
    case = {"id": "gates", "command": "danger.zone",
            "expect": {"status": "pending_confirmation"}}
    res = ev._run_case(_stub_repo(_Op()), case)
    assert res["status"] == "failed"
    assert any("PendingConfirmation" in e for e in res["errors"])


def test_pending_confirmation_case_fails_on_empty_token(monkeypatch):
    import sdk
    monkeypatch.setattr(sdk, "PendingConfirmation", _StubPending, raising=True)
    case = {"id": "gates", "command": "danger.zone",
            "expect": {"status": "pending_confirmation"}}
    res = ev._run_case(_stub_repo(_StubPending(token="")), case)
    assert res["status"] == "failed"
    assert any("token" in e for e in res["errors"])


class _StubOp:
    """带撤销粒度字段的 op 替身(单测 _assert_operation 新断言面)。"""

    def __init__(self, reverse_count=1, group_id="grp-1"):
        self._d = {
            "kind": "native",
            "patches": [{"entity_type": "t", "entity_id": "e1", "field": "x"}],
            "reverse_patches": [{"entity_type": "t", "entity_id": "e1", "field": "x"}][:reverse_count],
            "group_id": group_id,
        }

    def to_dict(self):
        return dict(self._d)


def test_reverse_patch_count_assertion_passes_and_fails():
    """撤销粒度断言面:reverse_patch_count 等值通过,不等必红(四步闸「撤销粒度不变」声明位)。"""
    good = {"id": "undo", "command": "x.y",
            "expect": {"status": "ok", "operation": {"reverse_patch_count": 1}}}
    res = ev._run_case(_stub_repo(_StubOp(reverse_count=1)), good)
    assert res["status"] == "passed", res

    res = ev._run_case(_stub_repo(_StubOp(reverse_count=0)), good)
    assert res["status"] == "failed"
    assert any("reverse_patch_count" in e for e in res["errors"])


def test_group_id_present_assertion_both_directions():
    expect_present = {"id": "undo", "command": "x.y",
                      "expect": {"status": "ok", "operation": {"group_id_present": True}}}
    assert ev._run_case(_stub_repo(_StubOp(group_id="g1")), expect_present)["status"] == "passed"
    res = ev._run_case(_stub_repo(_StubOp(group_id="")), expect_present)
    assert res["status"] == "failed"
    assert any("group_id_present" in e for e in res["errors"])

    expect_absent = {"id": "undo", "command": "x.y",
                     "expect": {"status": "ok", "operation": {"group_id_present": False}}}
    assert ev._run_case(_stub_repo(_StubOp(group_id="")), expect_absent)["status"] == "passed"
    assert ev._run_case(_stub_repo(_StubOp(group_id="g1")), expect_absent)["status"] == "failed"
