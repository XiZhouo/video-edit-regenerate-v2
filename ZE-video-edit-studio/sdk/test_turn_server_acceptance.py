"""§7.5 P0-E 验收(server 级):Turn 记录流 + /api/ai/turns 端点的守卫与归属。

覆盖我 shipped 但零 server 级测试的接缝:_chat_turn_start/_chat_turn_changeset/_chat_turn_end
(一轮 = 用户消息→本轮 AI 改动聚合 change_set[可逆/不可逆拆分]→助手回话,状态机 acting→completed)
+ ai_turns(访客空、归属过滤不越权、列表头 vs 按 id 全量)。协议内核本身在 test_assistant_turn 覆盖,
此处补「server 把内核接进 HTTP 半边」的行为。不起真 agentic 子进程(只测记录/读取逻辑)。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _d in (_ROOT, _ROOT / "01-core" / "01-data-structure" / "02-entity"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import server  # noqa: E402
from entity import Actor  # noqa: E402
from sdk.assistant_turn import (  # noqa: E402
    TurnStore, ACTING, COMPLETED, FAILED,
    EV_MESSAGE, EV_CHANGE_SET,
)


class _FakeOpLog:
    """最小 op_log:_chat_turn_changeset 只用 __len__(卡游标)与 entries()(取窗口)。"""
    def __init__(self, rows):
        self._rows = list(rows)
    def __len__(self):
        return len(self._rows)
    def entries(self):
        return [dict(r) for r in self._rows]


def _op_row(op_id, *, source="ai", command_id="entity.update", external=None, error=None):
    op = {"id": op_id, "source": source, "command_id": command_id}
    if external is not None:
        op["external"] = external
    if error is not None:
        op["error"] = error
    return {"op": op}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = SimpleNamespace(turn_store=TurnStore(tmp_path / "turns.json"))
    monkeypatch.setattr(server, "_repo", r)
    # ai_turns 的 _require_tool_access 内部(登录/权限)在别处覆盖;此处 no-op 聚焦 guard+归属逻辑。
    monkeypatch.setattr(server, "_require_tool_access", lambda actor: None)
    return r


# ── Turn 记录流(§7.5:一轮多工具多实体改动 + 追溯)────────────────────────────

def test_chat_turn_flow_records_message_changeset_and_completion(repo):
    actor = Actor(user_id="uA")
    tid = server._chat_turn_start(actor, "把标题改成 Hello", project_id="proj-1")
    assert tid, "已登录用户应开出一轮"
    # 本轮 agentic 产两条 source='ai' op:一条纯实体改(可逆)+ 一条生成(带 external,不可逆)
    oplog = _FakeOpLog([
        _op_row("a" * 32, command_id="entity.update"),
        _op_row("g" * 32, command_id="image.generate",
                external={"kind": "ai_generation", "status": "ok"}),
    ])
    server._chat_turn_changeset(tid, oplog, 0)
    server._chat_turn_end(tid, ok=True, reply="改好了")

    t = repo.turn_store.get_turn(tid)
    assert t is not None and t.state == COMPLETED
    assert t.project_id == "proj-1"                       # §7.4 P0-D load ctx 前置
    kinds = [e.kind for e in t.events]
    assert kinds == [EV_MESSAGE, EV_CHANGE_SET, EV_MESSAGE]   # 用户消息→改动→助手回话,全可从 turn_id 追溯
    cs = next(e for e in t.events if e.kind == EV_CHANGE_SET).data
    assert cs["operation_ids"] == ["a" * 32, "g" * 32]
    assert cs["reversible"] == ["a" * 32]                # 纯实体改动可整轮撤销
    assert [i["operation_id"] for i in cs["irreversible"]] == ["g" * 32]   # 生成不可逆,列出不撤


def test_chat_turn_pure_answer_has_no_changeset(repo):
    # §7.5「一轮纯回答」:无 AI 改动 → 无 change_set 事件,仍正常收束
    actor = Actor(user_id="uA")
    tid = server._chat_turn_start(actor, "解释一下这个项目", project_id="proj-1")
    server._chat_turn_changeset(tid, _FakeOpLog([]), 0)   # 空窗口 → 不记 change_set
    server._chat_turn_end(tid, ok=True, reply="这是一个…")
    t = repo.turn_store.get_turn(tid)
    assert [e.kind for e in t.events] == [EV_MESSAGE, EV_MESSAGE]   # 只有两条消息
    assert t.state == COMPLETED


def test_chat_turn_failure_path_records_partial_and_fails(repo):
    # §7.5 失败:跑挂前已产的部分改动也聚合(可逆),状态转 failed
    actor = Actor(user_id="uA")
    tid = server._chat_turn_start(actor, "改一批", project_id="proj-1")
    server._chat_turn_changeset(tid, _FakeOpLog([_op_row("a" * 32)]), 0)
    server._chat_turn_end(tid, ok=False)
    t = repo.turn_store.get_turn(tid)
    assert t.state == FAILED
    assert any(e.kind == EV_CHANGE_SET for e in t.events)   # 部分改动仍记(整轮撤销可用)


def test_chat_turn_guest_not_recorded(repo):
    # 访客(空 uid / '__' 前缀)不记 turn(best-effort 返 ''),绝不阻断对话
    assert server._chat_turn_start(Actor(user_id=""), "hi") == ""
    assert server._chat_turn_start(Actor(user_id="__demo_x"), "hi") == ""


def test_changeset_idempotent_across_success_and_retry(repo):
    # 幂等 event_id=changeset:{turn}:成功/失败路径都调、或重试,只落一条 change_set
    actor = Actor(user_id="uA")
    tid = server._chat_turn_start(actor, "改", project_id="p")
    oplog = _FakeOpLog([_op_row("a" * 32)])
    server._chat_turn_changeset(tid, oplog, 0)
    server._chat_turn_changeset(tid, oplog, 0)             # 再调(模拟成功后重复)
    t = repo.turn_store.get_turn(tid)
    assert sum(1 for e in t.events if e.kind == EV_CHANGE_SET) == 1


# ── /api/ai/turns 端点守卫与归属(§7.5:追溯 + 不越权)──────────────────────────

def test_ai_turns_guest_returns_empty(repo):
    # 访客只读放行(非 401)但返空:list 空、by-id 空
    assert server.ai_turns(limit=20, turn_id="", actor=Actor(user_id="")) == {"turns": []}
    assert server.ai_turns(limit=20, turn_id="x", actor=Actor(user_id="__demo")) == {"turn": None}


def test_ai_turns_ownership_filter(repo):
    a = repo.turn_store.create_turn(actor_user_id="uA", feature_id="assistant.chat", project_id="p")
    # uA 能按 id 读到自己的轮
    got = server.ai_turns(limit=20, turn_id=a.turn_id, actor=Actor(user_id="uA"))
    assert got["turn"] is not None and got["turn"]["turn_id"] == a.turn_id
    assert got["turn"]["project_id"] == "p"
    # uB 读 uA 的轮 → None(不泄漏存在性,防越权)
    other = server.ai_turns(limit=20, turn_id=a.turn_id, actor=Actor(user_id="uB"))
    assert other["turn"] is None


def test_ai_turns_list_returns_heads_only(repo):
    ts = repo.turn_store
    t1 = ts.create_turn(actor_user_id="uA", feature_id="assistant.chat")
    ts.append_event(t1.turn_id, EV_MESSAGE, {"role": "user", "content": "hi"})
    ts.create_turn(actor_user_id="uB", feature_id="assistant.chat")   # 他人轮不应出现
    out = server.ai_turns(limit=20, turn_id="", actor=Actor(user_id="uA"))
    assert [t["turn_id"] for t in out["turns"]] == [t1.turn_id]       # 只本人
    head = out["turns"][0]
    assert head["event_count"] == 1 and "events" not in head          # 列表态只出头,不展开 events


# ── §7.4 P0-D 整轮撤销预览(_plan_turn_undo + guard 冲突检测 + 预览端点)──────────

import threading  # noqa: E402


class _FakeEntity:
    def __init__(self, name="", data=None):
        self.name = name
        self._data = dict(data or {})
    def get(self, k, default=None):
        return self._data.get(k, default)


class _FakeDoc:
    def __init__(self, entities):
        self._e = dict(entities)               # {(et,eid): _FakeEntity}
    def find(self, et, eid):
        return self._e.get((et, eid))


class _FakeOpLogRows:
    def __init__(self, rows):
        self._rows = list(rows)
    def entries(self):
        return [dict(r) for r in self._rows]


class _FakeCtx:
    def __init__(self, doc, oplog):
        self.doc = doc
        self.op_log = oplog
        self.lock = threading.Lock()


def _update_op(op_id, et, eid, field, before, after):
    return {"op": {"id": op_id, "source": "ai", "command_id": "entity.update",
                   "patches": [{"entity_type": et, "entity_id": eid, "field": field,
                                "op": "set", "before": before, "after": after}]}}


def test_undo_guard_holds_field_unchanged_and_conflict():
    doc = _FakeDoc({("box", "b1"): _FakeEntity(data={"title": "新"})})
    assert server._undo_guard_holds(doc, {"kind": "field_unchanged", "entity_type": "box",
                                          "entity_id": "b1", "field": "title", "expect_after": "新"})
    # 现值被改成别的 → guard 不成立(撤销会覆盖后续改动)
    assert not server._undo_guard_holds(doc, {"kind": "field_unchanged", "entity_type": "box",
                                              "entity_id": "b1", "field": "title", "expect_after": "旧但期望新"})
    # 实体已不在 → 不能补偿
    assert not server._undo_guard_holds(_FakeDoc({}), {"kind": "entity_exists",
                                                       "entity_type": "box", "entity_id": "bx"})


def test_plan_turn_undo_will_revert_when_unchanged():
    # 本轮把 title 改 旧→新;现值仍是 新(未被后续覆盖)→ will_revert
    ctx = _FakeCtx(_FakeDoc({("box", "b1"): _FakeEntity(data={"title": "新"})}),
                   _FakeOpLogRows([_update_op("a" * 32, "box", "b1", "title", "旧", "新")]))
    out = server._plan_turn_undo(ctx, ["a" * 32])
    assert [w["op_id"] for w in out["will_revert"]] == ["a" * 32]
    assert out["conflict"] == [] and out["skipped"] == []


def test_plan_turn_undo_conflict_when_changed_since():
    # 现值被后续改成 更新的值(≠本轮 after)→ conflict(跳过,不覆盖他人改动)
    ctx = _FakeCtx(_FakeDoc({("box", "b1"): _FakeEntity(data={"title": "更新的"})}),
                   _FakeOpLogRows([_update_op("a" * 32, "box", "b1", "title", "旧", "新")]))
    out = server._plan_turn_undo(ctx, ["a" * 32])
    assert out["will_revert"] == []
    assert [c["op_id"] for c in out["conflict"]] == ["a" * 32]


def test_plan_turn_undo_skips_unsupported_op_type():
    # 删除生命周期 op → skipped(v1 不自动恢复)
    del_op = {"op": {"id": "d" * 32, "source": "ai", "command_id": "entity.delete",
                     "patches": [{"entity_type": "box", "entity_id": "b1", "field": "__entity__",
                                  "op": "delete", "before": {"snap": 1}, "after": None}]}}
    ctx = _FakeCtx(_FakeDoc({}), _FakeOpLogRows([del_op]))
    out = server._plan_turn_undo(ctx, ["d" * 32])
    assert [s["op_id"] for s in out["skipped"]] == ["d" * 32]


def test_undo_preview_guest_and_nonowner_empty(repo):
    empty = {"will_revert": [], "conflict": [], "skipped": [], "irreversible": [], "entities": []}
    assert server.ai_turn_undo_preview("x", actor=Actor(user_id="")) == empty
    a = repo.turn_store.create_turn(actor_user_id="uA", feature_id="assistant.chat", project_id="p")
    assert server.ai_turn_undo_preview(a.turn_id, actor=Actor(user_id="uB")) == empty   # 非本人


# ── §7.3 /api/ai/chat 响应带 change_set 摘要(变更卡免额外往返)──────────────────

def test_chat_turn_changeset_returns_payload_for_response(repo):
    tid = server._chat_turn_start(Actor(user_id="uA"), "改", project_id="p")
    oplog = _FakeOpLog([_op_row("a" * 32, command_id="entity.update")])
    payload = server._chat_turn_changeset(tid, oplog, 0)
    assert payload is not None and payload["operation_ids"] == ["a" * 32]   # 返 payload 供响应摘要
    assert server._chat_turn_changeset(tid, _FakeOpLog([]), 0) is None       # 空窗口 → None


def test_changeset_response_summary_compresses():
    payload = {"operation_ids": ["a" * 32, "g" * 32], "count": 2,
               "reversible": ["a" * 32], "irreversible": [{"operation_id": "g" * 32}],
               "entities": [{"entity_type": "box", "entity_id": "b1", "fields": ["title"], "lifecycle": False}]}
    s = server._changeset_response_summary(payload)
    assert s == {"count": 2, "reversible_count": 1, "irreversible_count": 1,
                 "entities": payload["entities"]}
    assert "operation_ids" not in s                          # 摘要不带完整 ids(那走 /api/ai/turns)
    assert server._changeset_response_summary(None) is None   # 纯回答轮 → None
    assert server._changeset_response_summary({"operation_ids": []}) is None


# ── §7.5「中途取消」:POST /api/ai/turn/{id}/cancel ───────────────────────────

def test_turn_cancel_moves_to_cancelled_and_records_outcome(repo):
    from sdk.assistant_turn import PLANNING, CANCELLED, EV_TURN_END
    t = repo.turn_store.create_turn(actor_user_id="uA", feature_id="assistant.chat", project_id="p")
    repo.turn_store.transition(t.turn_id, PLANNING)
    repo.turn_store.transition(t.turn_id, ACTING)

    res = server.ai_turn_cancel(t.turn_id, actor=Actor(user_id="uA"))
    # 逐键断言而不是整字典相等:取消接口后来加了确认释放的回执
    # (released_confirmations / release_failed),整字典 == 会因为「多了两个键」变红,
    # 而红在无辜一侧 —— 接口加返回字段是向后兼容的演进,不是回归。
    assert res["cancelled"] is True
    assert res["state"] == CANCELLED
    # 新键也要有口径,不能只是放宽:本例没有待确认,所以释放数为 0、且不该有释放失败。
    assert res["released_confirmations"] == 0
    assert res["release_failed"] == []

    got = repo.turn_store.get_turn(t.turn_id)
    assert got.state == CANCELLED
    ends = [e for e in got.events if e.kind == EV_TURN_END]
    assert len(ends) == 1 and ends[0].data["outcome"] == "cancelled"   # 结局留在事件流里可追溯


def test_turn_cancel_is_idempotent_on_terminal(repo):
    from sdk.assistant_turn import PLANNING, CANCELLED, EV_TURN_END
    t = repo.turn_store.create_turn(actor_user_id="uA", feature_id="assistant.chat")
    repo.turn_store.transition(t.turn_id, PLANNING)
    server.ai_turn_cancel(t.turn_id, actor=Actor(user_id="uA"))
    # 重复点取消:不报错、不重复记结局事件
    again = server.ai_turn_cancel(t.turn_id, actor=Actor(user_id="uA"))
    assert again["cancelled"] is False and again["state"] == CANCELLED
    got = repo.turn_store.get_turn(t.turn_id)
    assert len([e for e in got.events if e.kind == EV_TURN_END]) == 1


def test_turn_cancel_from_created_and_awaiting_confirmation(repo):
    """cancel 可从任一非终态发生(§7.4:取消随时可用,不必等它走到 acting)。"""
    from sdk.assistant_turn import PLANNING, AWAITING_CONFIRMATION, CANCELLED
    a = repo.turn_store.create_turn(actor_user_id="uA", feature_id="assistant.chat")
    assert server.ai_turn_cancel(a.turn_id, actor=Actor(user_id="uA"))["state"] == CANCELLED

    b = repo.turn_store.create_turn(actor_user_id="uA", feature_id="assistant.chat")
    repo.turn_store.transition(b.turn_id, PLANNING)
    repo.turn_store.transition(b.turn_id, AWAITING_CONFIRMATION)
    assert server.ai_turn_cancel(b.turn_id, actor=Actor(user_id="uA"))["state"] == CANCELLED


def test_turn_cancel_ownership_and_guest(repo):
    """非本人/不存在/访客一律 entity_not_found —— 不泄漏 turn 存在性(同 undo 口径)。"""
    from sdk import ZeError
    t = repo.turn_store.create_turn(actor_user_id="uA", feature_id="assistant.chat")
    for uid in ("uB", "", "__demo"):
        with pytest.raises(ZeError) as e:
            server.ai_turn_cancel(t.turn_id, actor=Actor(user_id=uid))
        assert e.value.code == "entity_not_found"
    with pytest.raises(ZeError):
        server.ai_turn_cancel("no-such-turn", actor=Actor(user_id="uA"))
