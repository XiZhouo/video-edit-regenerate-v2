"""test_realtime.py — RealtimeBroker + CommitCoordinator 集成测试.

覆盖:
  - Broker: publish / subscribe / unsubscribe / stats / replay buffer
  - 过滤: entity_type / entity_id / doc_id / 全订阅
  - since_seq: replay 行为
  - InMemoryTransport: deliver + callback 抛错隔离
  - 提交边界: CommandRegistry 不 append / publish；CommitCoordinator 仅在
    Document + OpLog 都可证明 durable 后发布 canonical event
  - WSTransport NotImplementedError 占位
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# v3 平铺: 注入 兄弟目录
_HERE = Path(__file__).resolve().parent
_CORE = _HERE.parent.parent
_PATHS = [
    _HERE,
    _CORE / "_protocol",
    _CORE / "01-data-structure" / "01-document",
    _CORE / "01-data-structure" / "02-entity",
    _CORE / "02-data-flow" / "07-persistence",
    _CORE / "03-commands-operations" / "01-command",
    _CORE / "03-commands-operations" / "02-operation",
]
for _p in _PATHS:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from broker import RealtimeBroker
from command import CommandRegistry, CommandSpec
from commit_contracts import (
    CommitFailure,
    CommitReceipt,
    CommitStage,
    CommitStatus,
    DurabilityFacts,
    DurabilityState,
    EventStatus,
)
from commit_coordinator import BrokerEventPublisher, CommitCoordinator
from document import Document
from entity import Actor, Entity
from operation import OpLog, Patch, new_operation
from transport import InMemoryTransport, Transport, WSTransport


# ==============================================================
# 辅助
# ==============================================================

def _make_payload(entity_type="t", entity_id="e1", doc_id=None,
                  change_kind="updated", source="user"):
    """造一个最简 entity_change payload."""
    return {
        "type": "entity_change",
        "doc_id": doc_id,
        "op_id": "op-1",
        "trace_id": "trace-1",
        "source": source,
        "command_id": "test",
        "actor": {"user_id": "u1"},
        "ts": 1.0,
        "changes": [{
            "entity_type": entity_type,
            "entity_id": entity_id,
            "change_kind": change_kind,
            "changed_fields": ["x"],
            "patches": [],
            "snapshot": {} if change_kind != "hard_removed" else None,
        }],
    }


# ==============================================================
# Transport
# ==============================================================

def test_transport_protocol_runtime_checkable():
    t = InMemoryTransport()
    assert isinstance(t, Transport)


def test_in_memory_transport_deliver():
    t = InMemoryTransport()
    received = []
    t.register_subscriber("s1", lambda p: received.append(p))
    t.deliver("s1", {"foo": "bar"})
    assert received == [{"foo": "bar"}]


def test_in_memory_transport_unregister_stops_delivery():
    t = InMemoryTransport()
    received = []
    t.register_subscriber("s1", lambda p: received.append(p))
    t.unregister("s1")
    t.deliver("s1", {"foo": "bar"})
    assert received == []


def test_in_memory_transport_deliver_to_unknown_sub_is_noop():
    t = InMemoryTransport()
    # 不应抛
    t.deliver("nonexistent", {"x": 1})


def test_in_memory_transport_callback_exception_is_swallowed():
    """callback 抛错应被 catch (一个坏 subscriber 不污染别的)."""
    t = InMemoryTransport()

    def bad_cb(p):
        raise RuntimeError("boom")

    received_good = []
    t.register_subscriber("bad", bad_cb)
    t.register_subscriber("good", lambda p: received_good.append(p))
    # 不应抛
    t.deliver("bad", {"x": 1})
    t.deliver("good", {"x": 1})
    assert received_good == [{"x": 1}]


def test_ws_transport_not_implemented():
    t = WSTransport()
    with pytest.raises(NotImplementedError):
        t.register_subscriber("s1", lambda p: None)
    with pytest.raises(NotImplementedError):
        t.deliver("s1", {})
    with pytest.raises(NotImplementedError):
        t.unregister("s1")


# ==============================================================
# Broker: publish + subscribe (基础)
# ==============================================================

def test_broker_publish_routes_to_subscriber():
    broker = RealtimeBroker()
    received = []
    broker.subscribe(callback=lambda p: received.append(p))
    seq = broker.publish_change(_make_payload())
    assert seq == 1
    assert len(received) == 1
    assert received[0]["type"] == "entity_change"


def test_broker_publish_returns_monotonic_seq():
    broker = RealtimeBroker()
    seqs = [broker.publish_change(_make_payload()) for _ in range(3)]
    assert seqs == [1, 2, 3]


def test_broker_delivered_payload_includes_seq():
    broker = RealtimeBroker()
    received = []
    seq = broker.publish_change(_make_payload())
    broker.subscribe(callback=lambda p: received.append(p), since_seq=seq)
    assert received[0]["seq"] == seq


def test_broker_multiple_subscribers_all_receive():
    broker = RealtimeBroker()
    r1, r2 = [], []
    broker.subscribe(callback=lambda p: r1.append(p))
    broker.subscribe(callback=lambda p: r2.append(p))
    broker.publish_change(_make_payload())
    assert len(r1) == 1
    assert len(r2) == 1


def test_broker_publish_non_dict_raises():
    broker = RealtimeBroker()
    with pytest.raises(TypeError):
        broker.publish_change("not a dict")  # type: ignore[arg-type]


# ==============================================================
# Broker: 过滤
# ==============================================================

def test_subscribe_entity_type_filter():
    broker = RealtimeBroker()
    received = []
    broker.subscribe(entity_type="entry", callback=lambda p: received.append(p))
    broker.publish_change(_make_payload(entity_type="entry"))
    broker.publish_change(_make_payload(entity_type="other"))
    assert len(received) == 1
    assert received[0]["changes"][0]["entity_type"] == "entry"


def test_subscribe_entity_id_filter():
    broker = RealtimeBroker()
    received = []
    broker.subscribe(
        entity_type="t", entity_id="e1",
        callback=lambda p: received.append(p),
    )
    broker.publish_change(_make_payload(entity_type="t", entity_id="e1"))
    broker.publish_change(_make_payload(entity_type="t", entity_id="e2"))
    assert len(received) == 1
    assert received[0]["changes"][0]["entity_id"] == "e1"


def test_subscribe_doc_id_filter():
    broker = RealtimeBroker()
    received = []
    broker.subscribe(doc_id="doc-A", callback=lambda p: received.append(p))
    broker.publish_change(_make_payload(doc_id="doc-A"))
    broker.publish_change(_make_payload(doc_id="doc-B"))
    assert len(received) == 1


def test_subscribe_all_no_filter():
    """全 None: 听一切."""
    broker = RealtimeBroker()
    received = []
    broker.subscribe(callback=lambda p: received.append(p))
    broker.publish_change(_make_payload(entity_type="a", entity_id="1"))
    broker.publish_change(_make_payload(entity_type="b", entity_id="2"))
    assert len(received) == 2


def test_subscribe_combined_filters():
    """entity_type + entity_id 都过滤."""
    broker = RealtimeBroker()
    received = []
    broker.subscribe(
        entity_type="t", entity_id="e1",
        callback=lambda p: received.append(p),
    )
    broker.publish_change(_make_payload(entity_type="other", entity_id="e1"))
    broker.publish_change(_make_payload(entity_type="t", entity_id="other"))
    broker.publish_change(_make_payload(entity_type="t", entity_id="e1"))
    assert len(received) == 1


# ==============================================================
# Broker: unsubscribe
# ==============================================================

def test_unsubscribe_stops_delivery():
    broker = RealtimeBroker()
    received = []
    sub = broker.subscribe(callback=lambda p: received.append(p))
    broker.publish_change(_make_payload())
    assert len(received) == 1
    sub.unsubscribe()
    broker.publish_change(_make_payload())
    assert len(received) == 1   # 没新增


def test_unsubscribe_idempotent():
    broker = RealtimeBroker()
    sub = broker.subscribe(callback=lambda p: None)
    sub.unsubscribe()
    sub.unsubscribe()   # 再调不抛


# ==============================================================
# Broker: replay buffer / since_seq
# ==============================================================

def test_since_seq_replay():
    broker = RealtimeBroker()
    # 先 publish 3 条
    broker.publish_change(_make_payload(entity_id="e1"))
    broker.publish_change(_make_payload(entity_id="e2"))
    broker.publish_change(_make_payload(entity_id="e3"))
    # 然后订阅 since_seq=2
    received = []
    broker.subscribe(
        callback=lambda p: received.append(p), since_seq=2,
    )
    # 应 replay 第 2, 3 条 (seq >= 2)
    assert len(received) == 2
    assert received[0]["changes"][0]["entity_id"] == "e2"
    assert received[1]["changes"][0]["entity_id"] == "e3"


def test_since_seq_replay_with_filter():
    """replay 时过滤生效."""
    broker = RealtimeBroker()
    broker.publish_change(_make_payload(entity_type="a", entity_id="1"))
    broker.publish_change(_make_payload(entity_type="b", entity_id="2"))
    broker.publish_change(_make_payload(entity_type="a", entity_id="3"))
    received = []
    broker.subscribe(
        entity_type="a",
        callback=lambda p: received.append(p),
        since_seq=1,
    )
    # 只应收 type=a 的 2 条
    assert len(received) == 2
    for p in received:
        assert p["changes"][0]["entity_type"] == "a"


def test_replay_buffer_evicts_oldest():
    broker = RealtimeBroker(replay_buffer_size=3)
    for i in range(5):
        broker.publish_change(_make_payload(entity_id=f"e{i}"))
    # buffer 仅 3 条 (seq 3, 4, 5)
    received = []
    sub = broker.subscribe(callback=lambda p: received.append(p), since_seq=1)
    # 静默给出 3..5 会把缺失的 1..2 伪装成连续历史；新合同显式要求 snapshot。
    assert sub.replay_gap is True
    assert sub.gap_reason == "replay_buffer_gap"
    assert received == []


def test_since_seq_future_returns_nothing():
    broker = RealtimeBroker()
    broker.publish_change(_make_payload())
    received = []
    broker.subscribe(
        callback=lambda p: received.append(p), since_seq=99,
    )
    assert received == []


def test_subscription_reports_authoritative_replay_buffer_gap():
    broker = RealtimeBroker(replay_buffer_size=2)
    for i in range(4):
        broker.publish_change(_make_payload(entity_id=f"e{i}"))
    received = []
    sub = broker.subscribe(callback=received.append, since_seq=1)
    assert sub.replay_gap is True
    assert sub.gap_reason == "replay_buffer_gap"
    assert sub.oldest_buffered_seq == 3
    assert sub.last_seq_at_subscribe == 4
    # A truncated suffix is more dangerous than no replay: caller must snapshot.
    assert received == []


def test_subscription_reports_cursor_ahead_instead_of_leaking_older_live_events():
    broker = RealtimeBroker()
    broker.publish_change(_make_payload(entity_id="one"))
    received = []
    sub = broker.subscribe(callback=received.append, since_seq=9)
    assert sub.replay_gap is True
    assert sub.gap_reason == "cursor_ahead"
    broker.publish_change(_make_payload(entity_id="two"))
    assert received == []


def test_replay_catch_up_barrier_orders_concurrent_live_publish_after_replay():
    import threading

    broker = RealtimeBroker()
    broker.publish_change(_make_payload(entity_id="one"))
    broker.publish_change(_make_payload(entity_id="two"))
    entered = threading.Event()
    release = threading.Event()
    received = []

    def callback(payload):
        if payload["seq"] == 1:
            entered.set()
            assert release.wait(timeout=2)
        received.append(payload["seq"])

    thread = threading.Thread(
        target=lambda: broker.subscribe(callback=callback, since_seq=1),
    )
    thread.start()
    assert entered.wait(timeout=2)
    # This publish happens while replay callback seq=1 is blocked.  It must be
    # queued after replay seq=2, not delivered immediately as seq=3,1,2.
    broker.publish_change(_make_payload(entity_id="three"))
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert received == [1, 2, 3]


def test_subscription_delivery_watermark_waits_for_inflight_callback():
    import threading

    broker = RealtimeBroker()
    entered = threading.Event()
    release = threading.Event()

    def callback(_payload):
        entered.set()
        assert release.wait(timeout=2)

    sub = broker.subscribe(callback=callback)
    publisher = threading.Thread(target=lambda: broker.publish_change(_make_payload()))
    publisher.start()
    assert entered.wait(timeout=2)
    assert sub.wait_until_delivered(1, timeout=0.01) is False
    release.set()
    publisher.join(timeout=2)
    assert not publisher.is_alive()
    assert sub.wait_until_delivered(1, timeout=0.1) is True


# ==============================================================
# Broker: stats
# ==============================================================

def test_stats():
    broker = RealtimeBroker()
    s1 = broker.subscribe(entity_type="a", callback=lambda p: None)
    s2 = broker.subscribe(entity_type="b", entity_id="x", callback=lambda p: None)
    broker.publish_change(_make_payload())
    broker.publish_change(_make_payload())
    stats = broker.stats()
    assert stats["n_subscribers"] == 2
    assert stats["n_messages"] == 2
    assert stats["last_seq"] == 2
    assert isinstance(stats["stream_id"], str) and len(stats["stream_id"]) == 32
    assert "entity:a:*" in stats["topics"]
    assert "entity:b:x" in stats["topics"]


def test_stats_after_unsubscribe():
    broker = RealtimeBroker()
    s = broker.subscribe(callback=lambda p: None)
    assert broker.stats()["n_subscribers"] == 1
    s.unsubscribe()
    assert broker.stats()["n_subscribers"] == 0


# ==============================================================
# Broker: callback 抛错隔离
# ==============================================================

def test_bad_callback_does_not_affect_others():
    broker = RealtimeBroker()
    good_received = []
    broker.subscribe(callback=lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    broker.subscribe(callback=lambda p: good_received.append(p))
    broker.publish_change(_make_payload())
    assert len(good_received) == 1


# ==============================================================
# CommandRegistry / CommitCoordinator 提交边界
# ==============================================================

def _register_bump_command(registry):
    def execute_fn(*, args, actor, source, doc):
        entity = doc.get("t", "e1")
        before = entity.get("x")
        entity.update({"x": 2}, expected_version=entity.version, actor=actor)
        return new_operation(
            command_id="bump", source=source, actor=actor.to_dict(),
            patches=[Patch("t", "e1", "x", "set", before, 2)],
            reverse_patches=[Patch("t", "e1", "x", "set", 2, before)],
        )

    registry.register(CommandSpec(id="bump", label="Bump", execute=execute_fn))


class _DurableStore:
    """最小 CommitStore：用 receipt 明确证明 Document + OpLog 均已落盘。"""

    def __init__(self, sequence=None):
        self.calls = []
        self.sequence = sequence

    def commit(self, intent):
        self.calls.append(intent)
        if self.sequence is not None:
            self.sequence.append("document+oplog:durable")
        return CommitReceipt.committed(
            commit_id=intent.commit_id,
            document_id=intent.document_id,
            operation_ids=intent.operation_ids,
            trace_id=intent.trace_id,
            save_policy=intent.save_policy,
            document_version=max(1, intent.document.version),
        )


class _IndeterminateStore:
    """模拟 Document 已 durable、OpLog 写入结果未知的危险边界。"""

    def commit(self, intent):
        failure = CommitFailure(
            stage=CommitStage.OPLOG,
            code="oplog_outcome_unknown",
            message="cannot prove whether the operation log append completed",
            facts=DurabilityFacts(
                mutation_applied=True,
                document=DurabilityState.DURABLE,
                oplog=DurabilityState.UNKNOWN,
            ),
        )
        return CommitReceipt.from_failure(
            commit_id=intent.commit_id,
            document_id=intent.document_id,
            operation_ids=intent.operation_ids,
            trace_id=intent.trace_id,
            save_policy=intent.save_policy,
            failure=failure,
            document_version=max(1, intent.document.version),
        )


def _command_fixture(*, legacy_broker=None):
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    registry = CommandRegistry(broker=legacy_broker)
    _register_bump_command(registry)
    actor = Actor(user_id="u1")
    return doc, registry, actor


def test_command_registry_does_not_append_or_publish_canonical_events():
    """旧 broker/op_log 参数也不能让纯执行器越过提交边界。"""
    broker = RealtimeBroker()
    received = []
    broker.subscribe(callback=received.append)
    doc, registry, actor = _command_fixture(legacy_broker=broker)
    legacy_log = OpLog()

    op = registry.dispatch(
        "bump", {}, actor=actor, source="user", doc=doc, op_log=legacy_log,
    )

    assert op.command_id == "bump"
    assert len(legacy_log) == 0
    assert received == []


def test_commit_coordinator_publishes_only_after_store_reports_both_durable():
    """canonical event 的前置事实是整笔 Document + OpLog durable receipt。"""
    sequence = []
    broker = RealtimeBroker()
    received = []

    def receive(payload):
        sequence.append("canonical:published")
        received.append(payload)

    broker.subscribe(callback=receive)
    doc, registry, actor = _command_fixture()
    store = _DurableStore(sequence)
    coordinator = CommitCoordinator(
        document=doc,
        store=store,
        publisher=BrokerEventPublisher(broker),
    )

    execution = coordinator.execute(
        lambda: registry.dispatch("bump", {}, actor=actor, source="user", doc=doc),
        trace_id="trace-durable",
    )

    assert sequence == ["document+oplog:durable", "canonical:published"]
    assert len(store.calls) == 1
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is EventStatus.PUBLISHED
    assert execution.receipt.document_durable is True
    assert execution.receipt.oplog_durable is True
    assert len(received) == 1
    payload = received[0]
    assert payload["type"] == "entity_change"
    assert payload["command_id"] == "bump"
    assert payload["commit_id"] == execution.receipt.commit_id
    assert payload["document_version"] == execution.receipt.document_version
    assert payload["trace_id"] == "trace-durable"


def test_commit_coordinator_never_publishes_indeterminate_storage_outcome():
    """OpLog 结果未知时不能对外声称变更已经成立，也不能自动重放。"""
    broker = RealtimeBroker()
    received = []
    broker.subscribe(callback=received.append)
    doc, registry, actor = _command_fixture()
    coordinator = CommitCoordinator(
        document=doc,
        store=_IndeterminateStore(),
        publisher=BrokerEventPublisher(broker),
    )

    execution = coordinator.execute(
        lambda: registry.dispatch("bump", {}, actor=actor, source="user", doc=doc),
    )

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.INDETERMINATE
    assert execution.receipt.retry_safe is False
    assert execution.events == ()
    assert received == []


def test_publish_failure_keeps_business_commit_and_marks_event_for_repair():
    """投递失败不能抹掉 durable 事实，更不能让 caller 重放业务命令。"""
    doc, registry, actor = _command_fixture()
    calls = []

    class BrokenPublisher:
        def publish(self, payload):
            calls.append("publish")
            raise RuntimeError("broker is broken")

    store = _DurableStore(calls)
    coordinator = CommitCoordinator(
        document=doc, store=store, publisher=BrokenPublisher(),
    )

    execution = coordinator.execute(
        lambda: registry.dispatch("bump", {}, actor=actor, source="user", doc=doc),
    )

    assert calls == ["document+oplog:durable", "publish"]
    assert len(store.calls) == 1
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is EventStatus.FAILED
    assert execution.receipt.retry_safe is False
    assert execution.receipt.failure is not None
    assert execution.receipt.failure.stage is CommitStage.EVENT


def test_durable_commit_without_live_publisher_returns_frozen_canonical_event():
    """无实时 transport 时也只产一次 canonical event，供 outbox/SSE 后续消费。"""
    doc, registry, actor = _command_fixture()
    store = _DurableStore()
    coordinator = CommitCoordinator(document=doc, store=store)

    execution = coordinator.execute(
        lambda: registry.dispatch("bump", {}, actor=actor, source="user", doc=doc),
        trace_id="trace-outbox",
    )

    assert execution.receipt is not None
    assert execution.receipt.event_status is EventStatus.NOT_REQUIRED
    assert len(execution.events) == 1
    payload = execution.events[0]
    assert payload["op_id"] == execution.result.id
    assert payload["commit_id"] == execution.receipt.commit_id
    assert payload["document_version"] == execution.receipt.document_version
    assert payload["trace_id"] == "trace-outbox"


def test_subscription_context_manager():
    """Subscription 作 context manager: 退出时自动 unsubscribe."""
    broker = RealtimeBroker()
    received = []
    with broker.subscribe(callback=lambda p: received.append(p)) as sub:
        broker.publish_change(_make_payload())
        assert len(received) == 1
    # 退出 with 后已 unsubscribe
    broker.publish_change(_make_payload())
    assert len(received) == 1


def test_stats_oldest_buffered_seq():
    """stats() 含 oldest_buffered_seq, 让 caller 判断 since_seq 是否丢消息."""
    broker = RealtimeBroker(replay_buffer_size=3)
    for _ in range(5):
        broker.publish_change(_make_payload())
    stats = broker.stats()
    # 5 个 publish, buffer 容 3, 应保留 seq=3,4,5
    assert stats["last_seq"] == 5
    assert stats["oldest_buffered_seq"] == 3
    assert stats["buffer_size"] == 3


def test_stats_oldest_buffered_seq_empty():
    broker = RealtimeBroker()
    s = broker.stats()
    assert s["oldest_buffered_seq"] == 0
    assert s["last_seq"] == 0


def test_reentrancy_publish_inside_callback_raises():
    """SPEC AP-R3: callback 里再调 publish_change 抛 RuntimeError (防死循环)."""
    broker = RealtimeBroker()

    def bad_callback(payload):
        # 在 callback 里再调 publish → 检测重入
        broker.publish_change(_make_payload())

    broker.subscribe(callback=bad_callback)
    # InMemoryTransport 会 catch callback 异常, 所以外部 publish 不抛.
    # 但内部第二次 publish 应触发 RuntimeError, 被 transport.deliver swallow.
    # 验证方式: 看 publish 后 stats.n_messages: 应只 +1 (内部那次没成功推进)
    broker.publish_change(_make_payload())
    assert broker.stats()["n_messages"] == 1


def test_thread_safe_subscribe_during_publish():
    """多线程并发 subscribe + publish; 锁应防止数据竞争."""
    import threading
    broker = RealtimeBroker()
    received = []
    stop = threading.Event()

    def publisher():
        while not stop.is_set():
            broker.publish_change(_make_payload())

    def subscriber():
        for _ in range(50):
            broker.subscribe(callback=lambda p: received.append(p))

    t_pub = threading.Thread(target=publisher)
    t_sub = threading.Thread(target=subscriber)
    t_pub.start()
    t_sub.start()
    t_sub.join()
    stop.set()
    t_pub.join()
    # 没有崩溃就算过 (具体收到数量取决于调度)
    assert broker.stats()["n_subscribers"] == 50
