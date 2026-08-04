"""test_concurrency.py — AdvisoryLock + QueueManager + RateLimiter 测试.

覆盖:
  - AdvisoryLock: acquire / release / state / 过期 / force / 自己续期 / sweep_expired
  - QueueManager: enqueue / position / priority / FIFO / ETA / start / finish
  - RateLimiter: configure / acquire / wait_for / available / 未配置默认放行
"""

from __future__ import annotations

import time

import pytest

from lock import AdvisoryLockManager, LockHandle
from task_queue import QueueManager, QueueItem
from rate import RateLimiter
from entity import ZeError


# ==============================================================
# AdvisoryLockManager — acquire / release
# ==============================================================

def test_lock_acquire_basic():
    mgr = AdvisoryLockManager()
    handle = mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1")
    assert handle.entity_type == "obj"
    assert handle.entity_id == "e1"
    assert handle.holder_user == "u1"
    assert handle.holder_device == "default"
    assert len(handle.lock_id) == 32


def test_lock_release_returns_true():
    mgr = AdvisoryLockManager()
    h = mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1")
    assert mgr.release(h) is True
    # 释放后 state 是 None
    assert mgr.state("obj", "e1") is None


def test_lock_release_wrong_handle_returns_false():
    """传一个不是当前持有的 handle, release 返 False (idempotent)."""
    mgr = AdvisoryLockManager()
    mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1")
    fake = LockHandle(
        lock_id="fake", entity_type="obj", entity_id="e1",
        holder_user="u1", holder_device="default",
        acquired_at=0, expires_at=999999,
    )
    assert mgr.release(fake) is False


def test_lock_blocks_other_user():
    """u1 拿了, u2 抢应该抛 acl_denied."""
    mgr = AdvisoryLockManager()
    mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1")
    with pytest.raises(ZeError) as exc:
        mgr.acquire(entity_type="obj", entity_id="e1", user_id="u2")
    assert exc.value.code == "acl_denied"
    assert exc.value.context["locked_by_user"] == "u1"


def test_lock_self_re_acquire_renews():
    """同 user+device 再 acquire = 续期, 不抛错."""
    mgr = AdvisoryLockManager()
    h1 = mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1", device_id="d1")
    h2 = mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1", device_id="d1")
    assert h2.expires_at >= h1.expires_at
    # h1 已被 h2 取代 (lock_id 变了); release h1 失败
    assert mgr.release(h1) is False
    # h2 能 release
    assert mgr.release(h2) is True


def test_lock_self_different_device_blocks():
    """同 user 不同 device → 当作不同 holder, 应被拒."""
    mgr = AdvisoryLockManager()
    mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1", device_id="d1")
    with pytest.raises(ZeError):
        mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1", device_id="d2")


def test_lock_force_takes_over():
    """force=True 强制接管."""
    mgr = AdvisoryLockManager()
    h1 = mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1")
    h2 = mgr.acquire(entity_type="obj", entity_id="e1", user_id="u2", force=True)
    assert h2.holder_user == "u2"
    # u1 的 handle 已失效
    assert mgr.release(h1) is False


# ==============================================================
# AdvisoryLock — state / TTL / sweep
# ==============================================================

def test_lock_state_returns_handle():
    mgr = AdvisoryLockManager()
    h = mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1")
    state = mgr.state("obj", "e1")
    assert state is not None
    assert state.lock_id == h.lock_id


def test_lock_state_missing_returns_none():
    mgr = AdvisoryLockManager()
    assert mgr.state("obj", "nope") is None


def test_lock_ttl_expiration():
    """短 TTL; sleep 后 state 自动清."""
    mgr = AdvisoryLockManager(ttl_sec=0.05)
    mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1")
    time.sleep(0.1)
    assert mgr.state("obj", "e1") is None


def test_lock_expired_can_be_re_acquired():
    """过期后别人能直接拿."""
    mgr = AdvisoryLockManager(ttl_sec=0.05)
    mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1")
    time.sleep(0.1)
    # u2 现在能拿 (无需 force)
    h = mgr.acquire(entity_type="obj", entity_id="e1", user_id="u2")
    assert h.holder_user == "u2"


def test_lock_sweep_expired_clears_count():
    mgr = AdvisoryLockManager(ttl_sec=0.05)
    mgr.acquire(entity_type="obj", entity_id="e1", user_id="u1")
    mgr.acquire(entity_type="obj", entity_id="e2", user_id="u1")
    mgr.acquire(entity_type="obj", entity_id="e3", user_id="u1")
    assert len(mgr) == 3
    time.sleep(0.1)
    cleared = mgr.sweep_expired()
    assert cleared == 3
    assert len(mgr) == 0


def test_lock_invalid_ttl_raises():
    with pytest.raises(ZeError):
        AdvisoryLockManager(ttl_sec=0)
    with pytest.raises(ZeError):
        AdvisoryLockManager(ttl_sec=-1)


# ==============================================================
# QueueManager — enqueue / position / FIFO / priority
# ==============================================================

def test_queue_enqueue_basic():
    q = QueueManager()
    item = q.enqueue("stage-a")
    assert item.stage_id == "stage-a"
    assert item.priority == 0
    assert len(item.task_id) == 32


def test_queue_position_returns_1_indexed():
    q = QueueManager()
    item1 = q.enqueue("stage-a")
    item2 = q.enqueue("stage-a")
    item3 = q.enqueue("stage-a")
    assert q.position(item1.task_id) == 1
    assert q.position(item2.task_id) == 2
    assert q.position(item3.task_id) == 3


def test_queue_position_unknown_returns_none():
    q = QueueManager()
    assert q.position("nope") is None


def test_queue_priority_higher_first():
    """高 priority 后入队但应该排前面."""
    q = QueueManager()
    low = q.enqueue("s", priority=0)
    high = q.enqueue("s", priority=10)
    assert q.position(high.task_id) == 1
    assert q.position(low.task_id) == 2


def test_queue_same_priority_fifo():
    """同 priority 内严格 FIFO."""
    q = QueueManager()
    a = q.enqueue("s", priority=5)
    b = q.enqueue("s", priority=5)
    c = q.enqueue("s", priority=5)
    assert q.position(a.task_id) == 1
    assert q.position(b.task_id) == 2
    assert q.position(c.task_id) == 3


def test_queue_multi_stage_isolated():
    """不同 stage 的 queue 独立."""
    q = QueueManager()
    a1 = q.enqueue("stage-a")
    b1 = q.enqueue("stage-b")
    # 每个 stage 内都是 position 1
    assert q.position(a1.task_id) == 1
    assert q.position(b1.task_id) == 1


def test_queue_waiting_filter_by_stage():
    q = QueueManager()
    q.enqueue("a")
    q.enqueue("a")
    q.enqueue("b")
    assert len(q.waiting("a")) == 2
    assert len(q.waiting("b")) == 1
    assert len(q.waiting()) == 3   # 全部


# ==============================================================
# QueueManager — start / finish / running
# ==============================================================

def test_queue_start_moves_to_running():
    q = QueueManager()
    item = q.enqueue("s")
    assert q.start(item.task_id) is True
    assert q.position(item.task_id) is None   # 已不在队列
    assert q.running() == [(item.task_id, "s")]


def test_queue_start_unknown_returns_false():
    q = QueueManager()
    assert q.start("nope") is False


def test_queue_finish_removes_from_running():
    q = QueueManager()
    item = q.enqueue("s")
    q.start(item.task_id)
    assert q.finish(item.task_id, duration=2.5) is True
    assert q.running() == []


def test_queue_finish_unknown_returns_false():
    q = QueueManager()
    assert q.finish("nope") is False


def test_queue_finish_records_duration_for_eta():
    """完成的 duration 应该被记录, 后续 ETA 用得上."""
    q = QueueManager()
    # 跑几次模拟历史
    for d in [3.0, 4.0, 5.0, 6.0, 7.0]:
        item = q.enqueue("s")
        q.start(item.task_id)
        q.finish(item.task_id, duration=d)
    # 新入队的应该有 P90 估算 ETA
    new_item = q.enqueue("s")
    eta = q.estimate_eta_sec(new_item.task_id)
    # P90 of [3,4,5,6,7] ~ 7 (索引 int(0.9*5)=4 → 第 5 个 → 7)
    assert eta == 7.0


# ==============================================================
# QueueManager — ETA
# ==============================================================

def test_eta_initial_fallback():
    """无历史样本 fallback 5 秒."""
    q = QueueManager()
    item = q.enqueue("s")
    assert q.estimate_eta_sec(item.task_id) == 5.0


def test_eta_scales_with_position():
    """ETA = position × P90."""
    q = QueueManager()
    # 跑两次, 每次耗时 2 秒
    for _ in range(2):
        item = q.enqueue("s")
        q.start(item.task_id)
        q.finish(item.task_id, duration=2.0)
    # 新入 3 个
    items = [q.enqueue("s") for _ in range(3)]
    # P90 of [2.0, 2.0] = 2.0
    assert q.estimate_eta_sec(items[0].task_id) == 2.0
    assert q.estimate_eta_sec(items[1].task_id) == 4.0
    assert q.estimate_eta_sec(items[2].task_id) == 6.0


def test_eta_unknown_returns_zero():
    q = QueueManager()
    assert q.estimate_eta_sec("nope") == 0.0


def test_stage_avg_duration_no_samples_returns_none():
    q = QueueManager()
    assert q.stage_avg_duration("s") is None


# ==============================================================
# RateLimiter
# ==============================================================

def test_rate_unconfigured_defaults_allow():
    """未配置 key 默认放行."""
    rl = RateLimiter()
    assert rl.acquire("never-configured") is True
    assert rl.available("never-configured") == float("inf")


def test_rate_configure_then_acquire():
    rl = RateLimiter()
    rl.configure("api", rate=10.0, capacity=5.0)
    # capacity=5 满桶起步
    for _ in range(5):
        assert rl.acquire("api") is True
    # 第 6 个失败 (token 用光, refill 还没到)
    assert rl.acquire("api") is False


def test_rate_refill_after_time():
    """sleep 后 token 应该补充."""
    rl = RateLimiter()
    rl.configure("api", rate=100.0, capacity=2.0)  # 每秒 100 个, 桶 2
    rl.acquire("api"); rl.acquire("api")
    assert rl.acquire("api") is False    # 桶空了
    time.sleep(0.05)                       # 0.05 * 100 = 5 个 token; clamped to 2
    assert rl.acquire("api") is True
    assert rl.acquire("api") is True


def test_rate_invalid_config_raises():
    rl = RateLimiter()
    with pytest.raises(ZeError):
        rl.configure("api", rate=0, capacity=5)
    with pytest.raises(ZeError):
        rl.configure("api", rate=-1, capacity=5)
    with pytest.raises(ZeError):
        rl.configure("api", rate=1, capacity=0)


def test_rate_acquire_zero_n_always_true():
    """acquire 0 个 token 立即返 True."""
    rl = RateLimiter()
    rl.configure("api", rate=1, capacity=1)
    rl.acquire("api")    # 用光
    assert rl.acquire("api", n=0) is True


def test_rate_acquire_partial_n_works():
    """acquire 浮点 n."""
    rl = RateLimiter()
    rl.configure("api", rate=1.0, capacity=10.0)
    assert rl.acquire("api", n=3.5) is True
    assert rl.available("api") < 10.0


def test_rate_wait_for_success_within_timeout():
    rl = RateLimiter()
    rl.configure("api", rate=100.0, capacity=1.0)
    rl.acquire("api")                       # 用光
    # 等 token 补充
    assert rl.wait_for("api", timeout=1.0) is True


def test_rate_wait_for_timeout():
    rl = RateLimiter()
    rl.configure("api", rate=0.5, capacity=1.0)  # 每 2 秒 1 个
    rl.acquire("api")
    # 短 timeout 应该失败
    start = time.time()
    result = rl.wait_for("api", timeout=0.15)
    elapsed = time.time() - start
    assert result is False
    assert elapsed >= 0.15
    assert elapsed < 0.5    # 不应该等太久


def test_rate_wait_for_unconfigured_immediate_true():
    rl = RateLimiter()
    assert rl.wait_for("never-configured", timeout=10.0) is True


def test_rate_available_decreases_after_acquire():
    rl = RateLimiter()
    rl.configure("api", rate=1.0, capacity=10.0)
    before = rl.available("api")
    rl.acquire("api", n=3.0)
    after = rl.available("api")
    assert before - after >= 2.9        # 允许 refill 误差


def test_rate_reconfigure_resets_bucket():
    """重 configure 应重置 tokens 到 capacity."""
    rl = RateLimiter()
    rl.configure("api", rate=1.0, capacity=5.0)
    rl.acquire("api", n=4.0)             # 几乎用光
    rl.configure("api", rate=1.0, capacity=5.0)
    assert rl.available("api") >= 4.9    # 重置满桶


def test_rate_is_configured():
    rl = RateLimiter()
    assert rl.is_configured("api") is False
    rl.configure("api", rate=1, capacity=1)
    assert rl.is_configured("api") is True
