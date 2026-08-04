"""test_confirm_gate — 命令确认门控(P1 §6#6)。

覆盖:
  - requires_confirm=False(默认)→ dispatch 直接执行返回 Operation(back-compat 回归)
  - requires_confirm=True 未 confirmed → 返回 PendingConfirmation,且 execute 未调
  - reserve→confirm→consume 后 token 单次使用；producer 前不提前弹出
  - dispatch / confirm 兼容接收 op_log/broker，但不 append/不发布
  - confirm 那一刻重过 poll(防 stale:确认时前置须仍成立)
  - confirm 用冻结快照的 args(确认时不可偷换参数)
  - cancel(token, actor=...) 丢弃本人待确认、不执行
"""
from __future__ import annotations

import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from command import CommandRegistry, CommandSpec, _render_confirm
from entity import Actor, ZeError
from operation import (
    Operation, OpLog, PendingConfirmation, PendingStore,
    new_operation, new_pending_confirmation,
)


def test_render_confirm_is_safe_substitution_not_format():
    # normal placeholder substitution works
    assert _render_confirm("do {what} x{n}", {"what": "gen", "n": 3}) == "do gen x3"
    # missing key -> placeholder kept verbatim (no crash)
    assert _render_confirm("hi {missing}", {}) == "hi {missing}"
    # NO format mini-language: attribute/index traversal in template is NOT evaluated
    assert _render_confirm("{x.__class__}", {"x": "s"}) == "{x.__class__}"
    assert _render_confirm("{x[0]}", {"x": "abc"}) == "{x[0]}"
    # empty template -> empty
    assert _render_confirm("", {"x": 1}) == ""


def _spec(reg, calls, *, requires_confirm=False, poll=None, template=""):
    def exe(args, actor, source, doc):
        calls.append(dict(args))
        return new_operation(command_id="c", source=source, actor=actor.to_dict(),
                             patches=[], reverse_patches=[])
    reg.register(CommandSpec(id="c", label="C", requires_confirm=requires_confirm,
                             confirm_template=template, poll=poll, execute=exe))


class _SpyBroker:
    def __init__(self):
        self.published = []

    def publish_change(self, payload):
        self.published.append(payload)


def _reserve(reg, pending, actor):
    return reg.reserve_confirmation(
        pending.token,
        actor=actor,
        approval_channel="human_http",
        approval_nonce=pending.approval_nonce,
    )


def test_no_confirm_executes_directly():
    reg = CommandRegistry()
    calls = []
    _spec(reg, calls)
    op = reg.dispatch("c", {"k": 1}, actor=Actor(user_id="u"), source="user", doc=None)
    assert isinstance(op, Operation)
    assert calls == [{"k": 1}]


def test_executed_dispatch_does_not_append_or_publish():
    broker = _SpyBroker()
    reg = CommandRegistry(broker=broker)
    calls = []
    _spec(reg, calls)
    log = OpLog()
    op = reg.dispatch(
        "c", {"k": 1}, actor=Actor(user_id="u"), source="user", doc=None,
        op_log=log,
    )
    assert isinstance(op, Operation)
    assert calls == [{"k": 1}]
    assert len(log) == 0
    assert broker.published == []


def test_requires_confirm_returns_pending_without_side_effects():
    broker = _SpyBroker()
    reg = CommandRegistry(broker=broker)
    calls = []
    _spec(reg, calls, requires_confirm=True, template="do {what}?")
    log = OpLog()
    r = reg.dispatch("c", {"what": "thing"}, actor=Actor(user_id="u"),
                     source="user", doc=None, op_log=log)
    assert isinstance(r, PendingConfirmation)
    assert r.summary == "do thing?"
    assert calls == []            # execute not called
    assert len(log) == 0          # nothing appended
    assert broker.published == []  # nothing broadcast
    assert len(reg.pending_store) == 1


def test_confirm_executes_and_token_is_single_use():
    broker = _SpyBroker()
    reg = CommandRegistry(broker=broker)
    calls = []
    _spec(reg, calls, requires_confirm=True)
    a = Actor(user_id="u")
    log = OpLog()
    pending = reg.dispatch("c", {"k": 2}, actor=a, source="user", doc=None, op_log=log)
    reservation = _reserve(reg, pending, a)
    op = reg.confirm(reservation, actor=a, doc=None, op_log=log)
    reg.pending_store.consume(reservation)
    assert isinstance(op, Operation)
    assert calls == [{"k": 2}]
    assert len(log) == 0
    assert broker.published == []
    # token consumed -> reuse rejected
    with pytest.raises(ZeError) as ei:
        _reserve(reg, pending, a)
    assert ei.value.code == "confirm_token_unknown"


def test_confirm_uses_frozen_args_not_caller_supplied():
    reg = CommandRegistry()
    calls = []
    _spec(reg, calls, requires_confirm=True)
    a = Actor(user_id="u")
    pending = reg.dispatch("c", {"original": True}, actor=a, source="user", doc=None)
    # confirm cannot smuggle different args; it replays the frozen snapshot
    reservation = _reserve(reg, pending, a)
    reg.confirm(reservation, actor=a, doc=None)
    reg.pending_store.consume(reservation)
    assert calls == [{"original": True}]


def test_returned_pending_cannot_mutate_nested_frozen_store_args():
    reg = CommandRegistry()
    calls = []
    _spec(reg, calls, requires_confirm=True)
    actor = Actor(user_id="u")
    args = {"nested": {"value": 1}}
    pending = reg.dispatch("c", args, actor=actor, source="user", doc=None)
    args["nested"]["value"] = 2
    pending.args["nested"]["value"] = 3

    reservation = _reserve(reg, pending, actor)
    reg.confirm(reservation, actor=actor, doc=None)
    reg.pending_store.consume(reservation)

    assert calls == [{"nested": {"value": 1}}]


def test_confirm_rechecks_poll_at_confirm_time():
    reg = CommandRegistry()
    calls = []
    gate = {"open": True}
    _spec(reg, calls, requires_confirm=True, poll=lambda args: gate["open"])
    a = Actor(user_id="u")
    pending = reg.dispatch("c", {}, actor=a, source="user", doc=None)
    gate["open"] = False  # precondition no longer holds at confirm time
    with pytest.raises(ZeError) as ei:
        reservation = _reserve(reg, pending, a)
        reg.confirm(reservation, actor=a, doc=None)
    assert ei.value.code == "poll_failed"
    assert calls == []
    reg.pending_store.release(reservation)
    assert reg.pending_store.state(pending.token) == "pending"


def test_cancel_drops_pending_without_executing():
    reg = CommandRegistry()
    calls = []
    _spec(reg, calls, requires_confirm=True)
    a = Actor(user_id="u")
    pending = reg.dispatch("c", {}, actor=a, source="user", doc=None)
    assert reg.pending_store.cancel(pending.token, actor=a) is True
    assert reg.pending_store.cancel(pending.token, actor=a) is False  # already gone
    assert calls == []
    with pytest.raises(ZeError):
        _reserve(reg, pending, a)


def test_confirm_is_actor_bound_and_only_one_reservation_can_run():
    reg = CommandRegistry()
    calls = []
    _spec(reg, calls, requires_confirm=True)
    owner = Actor(user_id="owner")
    pending = reg.dispatch("c", {}, actor=owner, source="user", doc=None)

    with pytest.raises(ZeError) as mismatch:
        _reserve(reg, pending, Actor(user_id="other"))
    assert mismatch.value.code == "confirmation_actor_mismatch"

    reservation = _reserve(reg, pending, owner)
    with pytest.raises(ZeError) as duplicate:
        _reserve(reg, pending, owner)
    assert duplicate.value.code == "confirmation_in_progress"
    with pytest.raises(ZeError) as cancelling:
        reg.pending_store.cancel(pending.token, actor=owner)
    assert cancelling.value.code == "confirmation_in_progress"
    reg.pending_store.release(reservation)


def test_concurrent_confirmation_has_exactly_one_reservation_owner():
    reg = CommandRegistry()
    calls = []
    _spec(reg, calls, requires_confirm=True)
    actor = Actor(user_id="owner")
    pending = reg.dispatch("c", {}, actor=actor, source="user", doc=None)
    barrier = Barrier(2)

    def reserve_once():
        barrier.wait()
        try:
            return _reserve(reg, pending, actor)
        except ZeError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: reserve_once(), range(2)))

    reservations = [result for result in results if not isinstance(result, str)]
    assert len(reservations) == 1
    assert [result for result in results if isinstance(result, str)] == [
        "confirmation_in_progress",
    ]
    reg.pending_store.release(reservations[0])


def test_pending_store_has_per_actor_capacity_and_expires_unreserved_entries():
    store = PendingStore(max_entries=2, max_per_actor=1, pending_ttl_sec=0.01)
    first = new_pending_confirmation(
        command_id="c", args={}, source="user", actor={"user_id": "u"},
    )
    store.put(first)
    second = new_pending_confirmation(
        command_id="c", args={}, source="user", actor={"user_id": "u"},
    )
    with pytest.raises(ZeError) as caught:
        store.put(second)
    assert caught.value.code == "confirmation_capacity_exceeded"

    import time
    time.sleep(0.02)
    store.put(second)
    assert store.state(first.token) is None
    assert store.state(second.token) == "pending"
