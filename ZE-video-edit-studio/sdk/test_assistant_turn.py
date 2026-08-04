"""Turn 协议内核测试(§7.1 P0-A):状态机合法性、事件幂等、断线恢复、turn 不串、落盘。"""

from __future__ import annotations

import pytest

from sdk.assistant_turn import (
    TurnStore, TurnError, can_transition, is_terminal,
    CREATED, PLANNING, ACTING, AWAITING_CONFIRMATION, COMPLETED, CANCELLED, FAILED,
    EV_MESSAGE, EV_TOOL_CALL, EV_TOOL_RESULT, EV_CHANGE_SET, EV_CONFIRMATION, EV_TURN_END,
    confirmation_event_from_pending, _CONFIRMATION_FORBIDDEN_KEYS,
)


def _pending_public(**over):
    p = {"type": "pending_confirmation", "doc_id": "d1", "token": "tok-9",
         "command_id": "image.regenerate", "source": "ai", "summary": "重生成 3 张图",
         "ts": 1.0, "actor": {"user_id": "u1"},
         "entity_ref": {"entity_type": "image_task", "entity_id": "t7"}}
    p.update(over)
    return p


def test_confirmation_event_maps_pending_fields():
    ev = confirmation_event_from_pending(_pending_public())
    assert ev["token"] == "tok-9" and ev["command_id"] == "image.regenerate"
    assert ev["source"] == "ai" and ev["summary"] == "重生成 3 张图"
    assert ev["entity_ref"] == {"entity_type": "image_task", "entity_id": "t7"}
    assert ev["status"] == "pending"


def test_confirmation_event_never_leaks_nonce_or_args():
    # 安全红线:即便 pending 数据里混入 approval_nonce/nonce/args,EV_CONFIRMATION 载荷绝不含它们(值也不泄)
    poisoned = _pending_public(approval_nonce="SECRET-NONCE", nonce="SECRET2",
                               args={"entity_type": "image_task", "prompt": "leak"})
    ev = confirmation_event_from_pending(poisoned)
    flat = repr(ev)
    for k in _CONFIRMATION_FORBIDDEN_KEYS:
        assert k not in ev
    assert "SECRET-NONCE" not in flat and "SECRET2" not in flat and "leak" not in flat


def test_confirmation_event_status_override():
    assert confirmation_event_from_pending(_pending_public(), status="approved")["status"] == "approved"
    assert confirmation_event_from_pending(_pending_public(), status="cancelled")["status"] == "cancelled"


def test_confirmation_event_entity_ref_none_when_absent():
    assert confirmation_event_from_pending(_pending_public(entity_ref=None))["entity_ref"] is None
    assert confirmation_event_from_pending(_pending_public(entity_ref={}))["entity_ref"] is None


def test_confirmation_event_non_dict_safe():
    ev = confirmation_event_from_pending("nope")
    assert ev["token"] == "" and ev["status"] == "pending" and ev["entity_ref"] is None


def test_confirmation_event_records_on_turn(tmp_path):
    # 端到端:构造载荷 → append EV_CONFIRMATION 到 turn(幂等键=token)→ awaiting_confirmation
    ts = _store(tmp_path)
    t = ts.create_turn(actor_user_id="u1", feature_id="assistant.chat")
    ts.transition(t.turn_id, PLANNING); ts.transition(t.turn_id, ACTING)
    ts.transition(t.turn_id, AWAITING_CONFIRMATION)
    ev = confirmation_event_from_pending(_pending_public())
    ts.append_event(t.turn_id, EV_CONFIRMATION, ev, event_id=f"confirm:{ev['token']}")
    ts.append_event(t.turn_id, EV_CONFIRMATION, ev, event_id=f"confirm:{ev['token']}")  # 幂等
    got = ts.get_turn(t.turn_id)
    confs = [e for e in got.events if e.kind == EV_CONFIRMATION]
    assert len(confs) == 1 and confs[0].data["token"] == "tok-9"


def _store(tmp_path):
    return TurnStore(tmp_path / "assistant_turns.json")


def test_state_machine_happy_path():
    # created→planning→acting→awaiting_confirmation→acting→completed 全合法
    chain = [(CREATED, PLANNING), (PLANNING, ACTING), (ACTING, AWAITING_CONFIRMATION),
             (AWAITING_CONFIRMATION, ACTING), (ACTING, COMPLETED)]
    for s, d in chain:
        assert can_transition(s, d), f"{s}→{d} 应合法"


def test_state_machine_rejects_illegal_and_terminal():
    assert not can_transition(CREATED, ACTING)          # 须先 planning
    assert not can_transition(AWAITING_CONFIRMATION, COMPLETED)  # 确认后须回 acting
    for term in (COMPLETED, CANCELLED, FAILED):
        assert is_terminal(term)
        for d in (PLANNING, ACTING, COMPLETED):
            assert not can_transition(term, d), f"终态 {term} 不应有出边"
    # cancel/fail 可从任一非终态
    for s in (CREATED, PLANNING, ACTING, AWAITING_CONFIRMATION):
        assert can_transition(s, CANCELLED) and can_transition(s, FAILED)


def test_create_turn_idempotent(tmp_path):
    st = _store(tmp_path)
    t1 = st.create_turn(actor_user_id="u1", feature_id="f", turn_id="T1")
    t2 = st.create_turn(actor_user_id="u1", feature_id="f", turn_id="T1")  # 同 id 重连
    assert t1.turn_id == t2.turn_id == "T1"
    assert t1.state == CREATED
    assert len(st.recent()) == 1                        # 不重开


def test_transition_persisted_and_illegal_rejected(tmp_path):
    st = _store(tmp_path)
    st.create_turn(actor_user_id="u", feature_id="f", turn_id="T")
    st.transition("T", PLANNING)
    st.transition("T", ACTING)
    assert st.get_turn("T").state == ACTING
    # 落盘重载后状态保持
    assert TurnStore(tmp_path / "assistant_turns.json").get_turn("T").state == ACTING
    with pytest.raises(TurnError):
        st.transition("T", CREATED)                     # acting→created 非法
    st.transition("T", COMPLETED)
    with pytest.raises(TurnError):
        st.transition("T", ACTING)                      # 终态无出边


def test_append_event_idempotent_and_seq(tmp_path):
    st = _store(tmp_path)
    st.create_turn(actor_user_id="u", feature_id="f", turn_id="T")
    e1 = st.append_event("T", EV_MESSAGE, {"role": "user", "content": "hi"}, event_id="E1")
    st.append_event("T", EV_MESSAGE, {"role": "user", "content": "DUP"}, event_id="E1")  # 断线重放
    st.append_event("T", EV_TOOL_CALL, {"tool": "entity.add"}, event_id="E2")
    evs = st.get_turn("T").events
    assert len(evs) == 2                                # E1 幂等未重加
    assert [e.seq for e in evs] == [0, 1]               # seq 单调
    assert e1.data["content"] == "hi"
    assert evs[0].data["content"] == "hi"               # 保留首值,非 DUP


def test_all_event_kinds_and_reconnect_recover(tmp_path):
    st = _store(tmp_path)
    st.create_turn(actor_user_id="u", feature_id="f", turn_id="T")
    for k in (EV_MESSAGE, EV_TOOL_CALL, EV_TOOL_RESULT, EV_CHANGE_SET, EV_CONFIRMATION, EV_TURN_END):
        st.append_event("T", k, {"k": k})
    # 断线重连:新 store 实例载回完整事件流
    recovered = TurnStore(tmp_path / "assistant_turns.json").get_turn("T")
    assert [e.kind for e in recovered.events] == [
        EV_MESSAGE, EV_TOOL_CALL, EV_TOOL_RESULT, EV_CHANGE_SET, EV_CONFIRMATION, EV_TURN_END]


def test_turns_do_not_cross(tmp_path):
    st = _store(tmp_path)
    st.create_turn(actor_user_id="u", feature_id="f", turn_id="A")
    st.create_turn(actor_user_id="u", feature_id="f", turn_id="B")
    st.append_event("A", EV_MESSAGE, {"n": 1})
    st.append_event("B", EV_MESSAGE, {"n": 2})
    st.append_event("A", EV_MESSAGE, {"n": 3})
    assert [e.data["n"] for e in st.get_turn("A").events] == [1, 3]  # B 的事件不串进 A
    assert [e.data["n"] for e in st.get_turn("B").events] == [2]
    assert [e.seq for e in st.get_turn("A").events] == [0, 1]        # 各自 seq


def test_recent_filter_and_order(tmp_path):
    st = _store(tmp_path)
    for i, (tid, u) in enumerate([("T1", "u1"), ("T2", "u2"), ("T3", "u1")]):
        st.create_turn(actor_user_id=u, feature_id="f", turn_id=tid)
    assert [t.turn_id for t in st.recent(actor_user_id="u1")] == ["T1", "T3"]
    assert [t.turn_id for t in st.recent(2)] == ["T2", "T3"]


def test_unknown_turn_and_kind_rejected(tmp_path):
    st = _store(tmp_path)
    with pytest.raises(TurnError):
        st.append_event("NOPE", EV_MESSAGE, {})
    st.create_turn(actor_user_id="u", feature_id="f", turn_id="T")
    with pytest.raises(TurnError):
        st.append_event("T", "bogus_kind", {})


# ── change_set 窗口聚合(§7.1 P0-D:整轮撤销的可逆单位)─────────────────────────

def _row(op_id, source="ai", command_id="entity.update", error=None):
    op = {"id": op_id, "source": source, "command_id": command_id}
    if error is not None:
        op["error"] = error
    return {"op": op}


def test_changeset_from_oplog_rows_filters_source_and_errors():
    from sdk.assistant_turn import changeset_from_oplog_rows
    rows = [
        _row("a" * 32, source="ai", command_id="entity.add"),
        _row("b" * 32, source="user", command_id="entity.update"),      # 非 AI → 排除
        _row("c" * 32, source="ai", command_id="entity.update"),
        _row("d" * 32, source="ai", command_id="entity.update", error={"code": "x"}),  # 失败 → 排除
        {"op": {"source": "ai"}},                                        # 无 id → 跳过
    ]
    payload = changeset_from_oplog_rows(rows)
    assert payload is not None
    assert payload["operation_ids"] == ["a" * 32, "c" * 32]              # 保持窗口序、只留合格 AI op
    assert payload["command_ids"] == ["entity.add", "entity.update"]     # 去重 + 排序
    assert payload["count"] == 2


def test_changeset_from_oplog_rows_empty_window_returns_none():
    from sdk.assistant_turn import changeset_from_oplog_rows
    assert changeset_from_oplog_rows([]) is None
    assert changeset_from_oplog_rows([_row("e" * 32, source="user")]) is None   # 窗口内无 AI op


def test_changeset_event_roundtrip_and_idempotent(tmp_path):
    from sdk.assistant_turn import changeset_from_oplog_rows
    ts = _store(tmp_path)
    t = ts.create_turn(actor_user_id="u1", feature_id="assistant.chat")
    payload = changeset_from_oplog_rows([_row("f" * 32, source="ai")])
    ts.append_event(t.turn_id, EV_CHANGE_SET, payload, event_id=f"changeset:{t.turn_id}")
    # 同 event_id 再记 → 幂等 no-op(成功/失败路径都调也只落一条)
    ts.append_event(t.turn_id, EV_CHANGE_SET, payload, event_id=f"changeset:{t.turn_id}")
    got = ts.get_turn(t.turn_id)
    cs = [e for e in got.events if e.kind == EV_CHANGE_SET]
    assert len(cs) == 1
    assert cs[0].data["operation_ids"] == ["f" * 32]
    assert cs[0].data["count"] == 1


# ── 可逆性拆分(§7.4 P0-D:整轮撤销只撤可逆实体改动,列出不可逆外部效果)──────────

def _op_row(op_id, command_id="entity.update", external=None):
    op = {"id": op_id, "source": "ai", "command_id": command_id}
    if external is not None:
        op["external"] = external
    return {"op": op}


def test_split_reversibility_entity_ops_are_reversible():
    from sdk.assistant_turn import split_changeset_reversibility
    ids = ["a" * 32, "b" * 32]
    rows = [_op_row("a" * 32, "entity.add"), _op_row("b" * 32, "entity.update")]
    out = split_changeset_reversibility(ids, rows)
    assert out["reversible"] == ["a" * 32, "b" * 32]      # 纯本地实体改动全可逆,保持窗口序
    assert out["irreversible"] == []
    assert out["reversible_count"] == 2 and out["irreversible_count"] == 0


def test_split_reversibility_external_ops_are_irreversible():
    from sdk.assistant_turn import split_changeset_reversibility
    ids = ["a" * 32, "g" * 32, "s" * 32]
    rows = [
        _op_row("a" * 32, "entity.update"),                                   # 纯实体 → 可逆
        _op_row("g" * 32, "image.generate", external={"kind": "ai_generation", "status": "ok"}),  # 生成 → 不可逆
        _op_row("s" * 32, "feishu.send", external={"kind": "outbound_api", "status": "ok"}),       # 外发 → 不可逆
    ]
    out = split_changeset_reversibility(ids, rows)
    assert out["reversible"] == ["a" * 32]
    assert {i["operation_id"] for i in out["irreversible"]} == {"g" * 32, "s" * 32}
    assert {i["kind"] for i in out["irreversible"]} == {"ai_generation", "outbound_api"}


def test_split_reversibility_receipts_surface_as_external_effects():
    from sdk.assistant_turn import split_changeset_reversibility
    receipts = [
        {"receipt_id": "r1", "provider": "gemini-api", "model_id": "gemini3",
         "capability": "image", "status": "ok", "cost_ref": {"est": "1img"}},
        {"receipt_id": "r2", "provider": "openai-api", "model_id": "gpt", "status": "error"},
    ]
    out = split_changeset_reversibility(["a" * 32], [_op_row("a" * 32)], receipts=receipts)
    assert len(out["external_effects"]) == 2                       # 轮级付费调用全列(charged 与 error 都算外部效果)
    assert out["external_effects"][0]["receipt_id"] == "r1"
    assert out["external_effects"][0]["cost_ref"] == {"est": "1img"}
    assert out["reversible"] == ["a" * 32]                         # receipt 不映射 op,可逆判定不受影响


def test_split_reversibility_op_outside_window_defaults_reversible():
    from sdk.assistant_turn import split_changeset_reversibility
    # operation_id 在 change_set 里但 oplog_rows 查不到(窗口外)→ 默认归可逆,caller revert 时找不到 op 自跳过
    out = split_changeset_reversibility(["z" * 32], [])
    assert out["reversible"] == ["z" * 32] and out["irreversible"] == []


def test_create_turn_records_project_id(tmp_path):
    ts = _store(tmp_path)
    t = ts.create_turn(actor_user_id="u1", feature_id="assistant.chat", project_id="proj-42")
    assert t.project_id == "proj-42"
    got = ts.get_turn(t.turn_id)                          # 落盘+载回保真
    assert got.project_id == "proj-42"
    assert got.public_dict()["project_id"] == "proj-42"


def test_turn_project_id_backward_compatible_default(tmp_path):
    ts = _store(tmp_path)
    t = ts.create_turn(actor_user_id="u1", feature_id="assistant.chat")   # 不传 → ""
    assert t.project_id == ""
    assert ts.get_turn(t.turn_id).project_id == ""


# ── 变更卡实体定位(§7.3:按本轮聚合、定位到实体/字段)────────────────────────────

def _op_row_p(op_id, patches, *, source="ai", error=None):
    op = {"id": op_id, "source": source, "command_id": "entity.update", "patches": patches}
    if error is not None:
        op["error"] = error
    return {"op": op}


def test_changeset_entities_groups_fields_by_entity():
    from sdk.assistant_turn import changeset_entities_from_oplog_rows
    rows = [
        _op_row_p("a" * 32, [{"entity_type": "box", "entity_id": "b1", "field": "name", "op": "set"},
                             {"entity_type": "box", "entity_id": "b1", "field": "color", "op": "set"}]),
        _op_row_p("c" * 32, [{"entity_type": "box", "entity_id": "b2", "field": "__entity__", "op": "create"}]),
    ]
    ents = changeset_entities_from_oplog_rows(rows)
    assert ents == [
        {"entity_type": "box", "entity_id": "b1", "fields": ["color", "name"], "lifecycle": False},
        {"entity_type": "box", "entity_id": "b2", "fields": [], "lifecycle": True},
    ]


def test_changeset_entities_skips_non_ai_and_errored():
    from sdk.assistant_turn import changeset_entities_from_oplog_rows
    rows = [
        _op_row_p("a" * 32, [{"entity_type": "box", "entity_id": "b1", "field": "name", "op": "set"}], source="user"),
        _op_row_p("b" * 32, [{"entity_type": "box", "entity_id": "b2", "field": "x", "op": "set"}], error={"code": "e"}),
    ]
    assert changeset_entities_from_oplog_rows(rows) == []       # 非 AI / 失败 op 不计入变更卡


def test_changeset_entities_dedups_across_ops_preserving_order():
    from sdk.assistant_turn import changeset_entities_from_oplog_rows
    rows = [
        _op_row_p("a" * 32, [{"entity_type": "t", "entity_id": "e2", "field": "f1", "op": "set"}]),
        _op_row_p("b" * 32, [{"entity_type": "t", "entity_id": "e1", "field": "f2", "op": "set"}]),
        _op_row_p("c" * 32, [{"entity_type": "t", "entity_id": "e2", "field": "f3", "op": "set"}]),  # e2 再现
    ]
    ents = changeset_entities_from_oplog_rows(rows)
    assert [e["entity_id"] for e in ents] == ["e2", "e1"]       # 按首现序,e2 合并 f1+f3
    assert ents[0]["fields"] == ["f1", "f3"]
