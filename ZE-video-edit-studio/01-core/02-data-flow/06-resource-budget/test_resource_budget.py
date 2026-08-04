"""Resource Budget 测试 — 含并发边界测试 + 运行时自证触发.

相对 05-concurrency 的 41 个单线程函数测试, 本文件的核心增量是 B 组「并发边界测试」:
真起线程压到上限, 断言峰值并发不超预算 / 不死锁 / 自证能触发. 所有 join 带超时, 绝不挂死.
"""
from __future__ import annotations

import threading
import time

import pytest

import resource_budget as rb     # 触发 sys.path 注入 (entity / rate)
from entity import ZeError       # noqa: E402  (resource_budget import 时已注入)


# ============================================================
# A. 单线程: 配置 / 探测 / 校验 / 审计 (照 capacity 测试风格)
# ============================================================

# ---- scope 命名 (原则 3) ----

@pytest.mark.parametrize("good", ["gpu.esrgan", "api.openai.chat", "cpu.pool", "io.disk.write", "mem.cache"])
def test_validate_scope_accepts_dotted(good):
    rb.validate_scope(good)     # 不抛


@pytest.mark.parametrize("bad", ["max_workers", "GPU.x", "gpu", "", "gpu.", ".gpu", "gpu..x",
                                 "a.b.c.d.e", "1gpu.x", "gpu.X"])
def test_validate_scope_rejects(bad):
    with pytest.raises(ZeError) as ei:
        rb.validate_scope(bad)
    assert ei.value.code == "resource_scope_invalid"


def test_scope_recommended_domain():
    assert rb.scope_is_recommended_domain("gpu.esrgan") is True
    assert rb.scope_is_recommended_domain("weird.thing") is False


# ---- 探测降级 (原则 2; 零重依赖) ----

def test_detect_cpu_count_fallback(monkeypatch):
    monkeypatch.setattr(rb.os, "cpu_count", lambda: None)
    assert rb.detect_cpu_count() == 1


def test_detect_has_gpu_force_env():
    assert rb.detect_has_gpu(env={"ZE_RB_FORCE_GPU": "1"}) is True
    assert rb.detect_has_gpu(env={"ZE_RB_FORCE_GPU": "true"}) is True
    assert rb.detect_has_gpu(env={"ZE_RB_FORCE_GPU": "0"}) is False


def test_detect_has_gpu_cuda_visible():
    assert rb.detect_has_gpu(env={"CUDA_VISIBLE_DEVICES": "0"}) is True
    assert rb.detect_has_gpu(env={"CUDA_VISIBLE_DEVICES": "-1"}) is False
    assert rb.detect_has_gpu(env={"CUDA_VISIBLE_DEVICES": ""}) is False


def test_detect_gpu_vram():
    assert rb.detect_gpu_vram_mb(env={}) is None
    assert rb.detect_gpu_vram_mb(env={"ZE_RB_GPU_VRAM_MB": "8192"}) == 8192
    assert rb.detect_gpu_vram_mb(env={"ZE_RB_GPU_VRAM_MB": "bad"}) is None


# ---- 工厂 + 覆盖优先级 (原则 2) ----

def test_for_gpu_model_default_one():
    c = rb.ResourceContract.for_gpu_model("gpu.esrgan", resource="ESRGAN", env={})
    assert c.max_concurrency == 1
    assert c.enforcement_scope == rb.ENFORCEMENT_MACHINE
    assert c.on_violation == rb.ON_WAIT


def test_for_cpu_pool_reserve(monkeypatch):
    monkeypatch.setattr(rb, "detect_cpu_count", lambda: 8)
    c = rb.ResourceContract.for_cpu_pool("cpu.pool", env={}, reserve=2)
    assert c.max_concurrency == 6


def test_env_override_beats_probe():
    c = rb.ResourceContract.for_gpu_model("gpu.esrgan",
                                          env={"ZE_RB_GPU_ESRGAN_MAX_CONCURRENCY": "4"})
    assert c.max_concurrency == 4


def test_external_api_env_only_downscales():
    # env 想上调到 10, 但合同上限 3 -> 取 min = 3
    c = rb.ResourceContract.for_external_api("api.x", rate_per_sec=5.0, max_concurrency=3,
                                             env={"ZE_RB_API_X_MAX_CONCURRENCY": "10"})
    assert c.max_concurrency == 3
    # env 下调到 1 -> 生效
    c2 = rb.ResourceContract.for_external_api("api.x", rate_per_sec=5.0, max_concurrency=3,
                                              env={"ZE_RB_API_X_MAX_CONCURRENCY": "1"})
    assert c2.max_concurrency == 1
    assert c2.on_violation == rb.ON_CANCEL_RETRY


def test_contract_invalid_values():
    with pytest.raises(ZeError) as e:
        rb.ResourceContract("X", "gpu.x", 0)
    assert e.value.code == "resource_budget_invalid"
    with pytest.raises(ZeError):
        rb.ResourceContract("X", "gpu.x", 1, enforcement_scope="cluster")
    with pytest.raises(ZeError):
        rb.ResourceContract("X", "gpu.x", 1, on_violation="explode")
    with pytest.raises(ZeError):
        rb.ResourceContract("X", "gpu.x", 1, rate_per_sec=-1.0)


def test_contract_bool_not_int():
    # bool 是 int 子类, 但 max_concurrency=True 不该被接受
    with pytest.raises(ZeError):
        rb.ResourceContract("X", "gpu.x", True)


def test_from_env_invalid_int():
    base = rb.ResourceContract("X", "gpu.x", 1)
    with pytest.raises(ZeError) as e:
        rb.ResourceContract.from_env(defaults=base,
                                     env={"ZE_RB_GPU_X_MAX_CONCURRENCY": "abc"})
    assert e.value.code == "resource_budget_invalid"


def test_from_env_zero_rejected():
    base = rb.ResourceContract("X", "gpu.x", 1)
    with pytest.raises(ZeError):
        rb.ResourceContract.from_env(defaults=base,
                                     env={"ZE_RB_GPU_X_MAX_CONCURRENCY": "0"})


# ---- 乘法审计 (原则 4) ----

def test_audit_multiplication_ok():
    cs = [rb.ResourceContract("A", "gpu.a", 8)]
    fs = [rb.ConcurrencyFactor("gpu.a", "p", path_count=2, per_path=2, sub=2)]  # 8
    reps = rb.audit_concurrency(cs, fs)
    assert reps[0].demanded == 8 and reps[0].ok


def test_audit_multiplication_exceeds():
    cs = [rb.ResourceContract("A", "gpu.a", 3)]
    fs = [rb.ConcurrencyFactor("gpu.a", "p1", path_count=2, per_path=2),  # 4
          rb.ConcurrencyFactor("gpu.a", "p2", path_count=1)]              # 1 -> total 5
    reps = rb.audit_concurrency(cs, fs)
    r = reps[0]
    assert r.demanded == 5 and not r.ok and r.reason == "exceeds_budget"
    with pytest.raises(ZeError) as e:
        rb.audit_concurrency(cs, fs, raise_on_exceed=True)
    assert e.value.code == "concurrency_audit_failed"


def test_audit_no_budget():
    reps = rb.audit_concurrency([], [rb.ConcurrencyFactor("gpu.x", "p", path_count=2)])
    assert not reps[0].ok and reps[0].reason == "no_budget"
    with pytest.raises(ZeError):
        rb.audit_concurrency([], [rb.ConcurrencyFactor("gpu.x", "p")], raise_on_exceed=True)


# ============================================================
# B. 并发边界 (真起线程压上限; 这是本叶子相对 05-concurrency 的核心增量)
# ============================================================

def _hammer(gate, n_threads, hold_sec, *, acquire_timeout=2.0):
    """起 n_threads 线程, 每个进 gate.acquire() with 体内 sleep hold_sec.
       barrier 让线程尽量同时起跑制造重叠; 返回 errors; 所有 join 带超时绝不挂死."""
    errors = []
    barrier = threading.Barrier(n_threads)

    def work():
        try:
            barrier.wait(timeout=5)
            with gate.acquire(timeout=acquire_timeout):
                time.sleep(hold_sec)
        except Exception as ex:        # noqa: BLE001 (测试收集所有异常)
            errors.append(ex)

    threads = [threading.Thread(target=work) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return errors, threads


def test_peak_never_exceeds_budget():
    gate = rb.ResourceGate(rb.ResourceContract("X", "gpu.x", 3))
    errors, threads = _hammer(gate, n_threads=20, hold_sec=0.05)
    assert all(not t.is_alive() for t in threads), "死锁: 有线程没在超时内结束"
    assert not errors, f"不该有异常: {errors}"
    assert gate.stats()["peak"] <= 3
    assert gate.stats()["active"] == 0


def test_max_one_truly_serializes():
    gate = rb.ResourceGate(rb.ResourceContract("X", "gpu.x", 1))
    errors, threads = _hammer(gate, n_threads=10, hold_sec=0.02)
    assert all(not t.is_alive() for t in threads)
    assert not errors, f"不该有异常: {errors}"
    assert gate.stats()["peak"] == 1     # max=1 必须真串行


def test_currently_inside_never_exceeds():
    gate = rb.ResourceGate(rb.ResourceContract("X", "gpu.x", 3))
    inside = {"n": 0, "max": 0}
    lock = threading.Lock()
    violations = []

    def work():
        try:
            with gate.acquire(timeout=3):
                with lock:
                    inside["n"] += 1
                    inside["max"] = max(inside["max"], inside["n"])
                    if inside["n"] > 3:
                        violations.append(inside["n"])
                time.sleep(0.03)
                with lock:
                    inside["n"] -= 1
        except Exception as ex:          # noqa: BLE001
            violations.append(repr(ex))

    threads = [threading.Thread(target=work) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert all(not t.is_alive() for t in threads)
    assert not violations, f"并发越界: {violations}"
    assert inside["max"] <= 3


@pytest.mark.slow   # C3:墙钟型(sleep/TTL),归 pytest-slow 门
def test_deadlock_detection_timeout():
    # max=1, 一个线程长占; 另一个 acquire(timeout=0.3) 应超时抛 resource_unavailable (而非挂死)
    gate = rb.ResourceGate(rb.ResourceContract("X", "gpu.x", 1, on_violation=rb.ON_REJECT))
    got = {}

    def holder():
        with gate.acquire():
            time.sleep(1.0)

    def late():
        try:
            with gate.acquire(timeout=0.3):
                pass
        except ZeError as e:
            got["code"] = e.code

    th = threading.Thread(target=holder)
    th.start()
    time.sleep(0.1)
    tl = threading.Thread(target=late)
    tl.start()
    th.join(timeout=10)
    tl.join(timeout=10)
    assert not th.is_alive() and not tl.is_alive()
    assert got.get("code") == "resource_unavailable"


def test_self_attestation_fires_on_misconfigured_gate():
    # 畸形闸门: budget=2 但 semaphore 容量被改成 5 -> 自证应抓住并抛 resource_budget_exceeded.
    # 价值: 不靠"相信代码写对了"; 闸门被错配时运行时能抓住.
    gate = rb.ResourceGate(rb.ResourceContract("X", "gpu.x", 2))
    gate._sem = threading.BoundedSemaphore(5)    # 故意错配 (semaphore 比预算大)
    errors = []
    barrier = threading.Barrier(5)

    def work():
        try:
            barrier.wait(timeout=5)
            with gate.acquire(timeout=2):
                time.sleep(0.3)
        except ZeError as e:
            errors.append(e.code)
        except Exception as e:           # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=work) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert all(not t.is_alive() for t in threads)
    assert "resource_budget_exceeded" in errors, f"自证没触发: {errors}"


def test_rate_and_concurrency_series():
    # rate=5/s + burst=1, max=10; 起 10 线程同时来 -> 速率门把通过节流, 跨度应 >= ~1s
    c = rb.ResourceContract("X", "api.x", 10, rate_per_sec=5.0, rate_burst=1.0)
    gate = rb.ResourceGate(c)
    times = []
    tlock = threading.Lock()

    def work():
        with gate.acquire(timeout=10):
            with tlock:
                times.append(time.time())
            time.sleep(0.01)

    threads = [threading.Thread(target=work) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert all(not t.is_alive() for t in threads)
    span = max(times) - min(times)
    assert span >= 1.0, f"速率门没生效, span={span}"


# ============================================================
# GateRegistry (原则 1: 唯一入口)
# ============================================================

def test_registry_unique_scope():
    reg = rb.GateRegistry()
    g = rb.ResourceGate(rb.ResourceContract("X", "gpu.x", 1))
    reg.register(g)
    with pytest.raises(ZeError) as e:
        reg.register(rb.ResourceGate(rb.ResourceContract("Y", "gpu.x", 2)))
    assert e.value.code == "resource_scope_duplicate"
    assert reg.get("gpu.x") is g


def test_registry_get_or_create_idempotent():
    reg = rb.GateRegistry()
    c = rb.ResourceContract("X", "gpu.x", 1, enforcement_scope=rb.ENFORCEMENT_PROCESS)
    g1 = reg.get_or_create(c)
    g2 = reg.get_or_create(c)
    assert g1 is g2
    assert isinstance(g1, rb.ResourceGate)


def test_registry_get_missing():
    reg = rb.GateRegistry()
    with pytest.raises(ZeError) as e:
        reg.get("gpu.nope")
    assert e.value.code == "resource_gate_not_found"


# ============================================================
# Probe (合同"验收"字段)
# ============================================================

def test_probe_from_gate():
    gate = rb.ResourceGate(rb.ResourceContract("X", "gpu.x", 2))
    probe = rb.Probe.from_gate(gate)
    assert probe.current() == 0
    probe.assert_within_budget()     # 不抛


def test_probe_assert_exceeds():
    probe = rb.Probe("gpu.x", 3, count_fn=lambda: 5)
    with pytest.raises(ZeError) as e:
        probe.assert_within_budget()
    assert e.value.code == "resource_budget_exceeded"


# ============================================================
# C. MachineGate (单机跨进程; flock 锁 OFD, 同进程不同 open 也互斥, 等价跨进程语义)
# ============================================================

@pytest.mark.skipif(not rb._HAS_FCNTL, reason="MachineGate 需要 fcntl (Unix only)")
def test_machine_gate_slot_limit(tmp_path):
    c = rb.ResourceContract("ESRGAN", "gpu.esrgan", 3,
                            enforcement_scope=rb.ENFORCEMENT_MACHINE)
    gate = rb.MachineGate(c, lock_dir=tmp_path)
    inside = {"n": 0, "max": 0}
    lock = threading.Lock()
    violations = []

    def work():
        try:
            with gate.acquire(timeout=5):
                with lock:
                    inside["n"] += 1
                    inside["max"] = max(inside["max"], inside["n"])
                    if inside["n"] > 3:
                        violations.append(inside["n"])
                time.sleep(0.05)
                with lock:
                    inside["n"] -= 1
        except Exception as ex:          # noqa: BLE001
            violations.append(repr(ex))

    threads = [threading.Thread(target=work) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)
    assert all(not t.is_alive() for t in threads)
    assert not violations, f"跨 slot 越界: {violations}"
    assert inside["max"] <= 3
    assert gate.held_count() == 0        # 全部释放


@pytest.mark.skipif(not rb._HAS_FCNTL, reason="fcntl only")
@pytest.mark.slow   # C3:墙钟型(sleep/TTL),归 pytest-slow 门
def test_machine_gate_reject_when_full(tmp_path):
    c = rb.ResourceContract("R", "gpu.r", 1, enforcement_scope=rb.ENFORCEMENT_MACHINE,
                            on_violation=rb.ON_REJECT)
    gate = rb.MachineGate(c, lock_dir=tmp_path)
    got = {}

    def holder():
        with gate.acquire():
            time.sleep(0.8)

    def late():
        try:
            with gate.acquire(timeout=0.3):
                pass
        except ZeError as e:
            got["code"] = e.code

    th = threading.Thread(target=holder)
    th.start()
    time.sleep(0.1)
    tl = threading.Thread(target=late)
    tl.start()
    th.join(timeout=10)
    tl.join(timeout=10)
    assert not th.is_alive() and not tl.is_alive()
    assert got.get("code") == "resource_unavailable"


# ============================================================
# D. 容量类 (CapacityGate / MachineCapacityGate; 加权预算 sum(cost) <= capacity)
# ============================================================

# ---- 合同 + 工厂 ----

def test_capacity_field_validation():
    rb.ResourceContract("V", "mem.vram", 1, capacity=8000)     # 不抛
    for bad in (0, -1, True):
        with pytest.raises(ZeError) as e:
            rb.ResourceContract("V", "mem.vram", 1, capacity=bad)
        assert e.value.code == "resource_budget_invalid"


def test_contract_kind_property():
    assert rb.ResourceContract("X", "gpu.x", 3).kind == "slot"
    assert rb.ResourceContract("V", "mem.vram", 1, capacity=8000).kind == "capacity"


def test_for_gpu_vram_factory():
    c = rb.ResourceContract.for_gpu_vram("mem.vram", per_job_mb=3000, total_mb=8000)
    assert c.kind == "capacity"
    assert c.capacity == 8000
    assert c.max_concurrency == 2                  # floor(8000/3000) 最坏并发数
    assert c.enforcement_scope == rb.ENFORCEMENT_MACHINE


def test_for_gpu_vram_uses_detect(monkeypatch):
    monkeypatch.setattr(rb, "detect_gpu_vram_mb", lambda env=None: 12000)
    c = rb.ResourceContract.for_gpu_vram("mem.vram", per_job_mb=4000)
    assert c.capacity == 12000 and c.max_concurrency == 3


def test_for_memory_pool_factory(monkeypatch):
    monkeypatch.setattr(rb, "detect_total_ram_mb", lambda: 16000)
    c = rb.ResourceContract.for_memory_pool("mem.rss", per_job_mb=2000, reserve_mb=1000)
    assert c.kind == "capacity"
    assert c.capacity == 15000                     # 16000 - 1000 reserve
    assert c.max_concurrency == 7                  # floor(15000/2000)


def test_capacity_env_only_downscales():
    c = rb.ResourceContract.for_capacity_pool("mem.vram", capacity=8000,
                                              env={"ZE_RB_MEM_VRAM_CAPACITY": "20000"})
    assert c.capacity == 8000                       # env 想上调 -> 取 min
    c2 = rb.ResourceContract.for_capacity_pool("mem.vram", capacity=8000,
                                               env={"ZE_RB_MEM_VRAM_CAPACITY": "4000"})
    assert c2.capacity == 4000                      # 下调生效


def test_detect_total_ram_mb_type():
    v = rb.detect_total_ram_mb()
    assert v is None or (isinstance(v, int) and v > 0)


# ---- CapacityGate 加权准入 (并发边界) ----

def _hammer_capacity(gate, cost, n_threads, hold_sec, *, acquire_timeout=3.0):
    """起 n_threads 线程, 每个 gate.acquire(cost=) with 体内 sleep; 记录在用量峰值; join 带超时."""
    used = {"cur": 0, "max": 0}
    lock = threading.Lock()
    errors = []
    barrier = threading.Barrier(n_threads)

    def work():
        try:
            barrier.wait(timeout=5)
            with gate.acquire(cost=cost, timeout=acquire_timeout):
                with lock:
                    used["cur"] += cost
                    used["max"] = max(used["max"], used["cur"])
                time.sleep(hold_sec)
                with lock:
                    used["cur"] -= cost
        except Exception as ex:        # noqa: BLE001
            errors.append(repr(ex))

    threads = [threading.Thread(target=work) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return used, errors, threads


def test_capacity_weighted_admission_never_exceeds():
    # cap=8000, 每任务 3000 -> 同时最多 2 个 (6000); 第 3 个必须等
    gate = rb.CapacityGate(rb.ResourceContract("V", "mem.vram", 2, capacity=8000))
    used, errors, threads = _hammer_capacity(gate, cost=3000, n_threads=10, hold_sec=0.05)
    assert all(not t.is_alive() for t in threads), "死锁"
    assert not errors, f"不该有异常: {errors}"
    assert used["max"] <= 8000, f"容量越界: {used['max']}"
    assert used["max"] == 6000, f"应恰好 2 个并行 (6000), got {used['max']}"
    assert gate.stats()["active"] == 0


def test_capacity_heterogeneous_costs():
    # 异构权重: 5000 + 3000 = 8000 可并存; 但不会出现 sum > 8000
    gate = rb.CapacityGate(rb.ResourceContract("V", "mem.vram", 1, capacity=8000))
    peak = {"v": 0}
    lock = threading.Lock()
    inuse = {"v": 0}
    errs = []

    def work(cost):
        try:
            with gate.acquire(cost=cost, timeout=5):
                with lock:
                    inuse["v"] += cost
                    peak["v"] = max(peak["v"], inuse["v"])
                    assert inuse["v"] <= 8000
                time.sleep(0.04)
                with lock:
                    inuse["v"] -= cost
        except Exception as ex:        # noqa: BLE001
            errs.append(repr(ex))

    costs = [5000, 3000, 5000, 3000, 2000, 6000, 4000, 4000]
    threads = [threading.Thread(target=work, args=(c,)) for c in costs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert all(not t.is_alive() for t in threads)
    assert not errs, f"越界或异常: {errs}"
    assert peak["v"] <= 8000


def test_capacity_cost_exceeds_capacity_rejected():
    gate = rb.CapacityGate(rb.ResourceContract("V", "mem.vram", 1, capacity=8000))
    with pytest.raises(ZeError) as e:
        with gate.acquire(cost=9000):
            pass
    assert e.value.code == "resource_budget_invalid"      # 单任务永远装不下


def test_capacity_cost_must_be_positive():
    gate = rb.CapacityGate(rb.ResourceContract("V", "mem.vram", 1, capacity=8000))
    for bad in (0, -5, True):
        with pytest.raises(ZeError) as e:
            with gate.acquire(cost=bad):
                pass
        assert e.value.code == "resource_budget_invalid"


@pytest.mark.slow   # C3:墙钟型(sleep/TTL),归 pytest-slow 门
def test_capacity_reject_when_full():
    gate = rb.CapacityGate(rb.ResourceContract("V", "mem.vram", 1, capacity=4000,
                                               on_violation=rb.ON_REJECT))
    got = {}

    def holder():
        with gate.acquire(cost=3000):
            time.sleep(0.8)

    def late():
        try:
            with gate.acquire(cost=3000, timeout=0.3):   # 6000 > 4000 -> 等不到
                pass
        except ZeError as e:
            got["code"] = e.code

    th = threading.Thread(target=holder)
    th.start()
    time.sleep(0.1)
    tl = threading.Thread(target=late)
    tl.start()
    th.join(timeout=10)
    tl.join(timeout=10)
    assert not th.is_alive() and not tl.is_alive()
    assert got.get("code") == "resource_unavailable"


def test_capacity_self_attestation_fires():
    # 畸形闸门: _available 被改得比 capacity 大 -> 过量准入 -> 自证抓住 resource_budget_exceeded
    gate = rb.CapacityGate(rb.ResourceContract("V", "mem.vram", 1, capacity=2))
    gate._available = 5         # 故意错配
    codes = []
    barrier = threading.Barrier(5)

    def work():
        try:
            barrier.wait(timeout=5)
            with gate.acquire(cost=1, timeout=2):
                time.sleep(0.2)
        except ZeError as e:
            codes.append(e.code)
        except Exception as e:   # noqa: BLE001
            codes.append(repr(e))

    threads = [threading.Thread(target=work) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert all(not t.is_alive() for t in threads)
    assert "resource_budget_exceeded" in codes, f"自证没触发: {codes}"


def test_capacity_gate_requires_capacity():
    with pytest.raises(ZeError) as e:
        rb.CapacityGate(rb.ResourceContract("X", "gpu.x", 3))    # 槽位合同没 capacity
    assert e.value.code == "resource_budget_invalid"


# ---- registry 路由 + 审计 + probe (容量) ----

def test_registry_routes_capacity_process():
    reg = rb.GateRegistry()
    c = rb.ResourceContract("V", "mem.vram", 1, capacity=8000,
                            enforcement_scope=rb.ENFORCEMENT_PROCESS)
    g = reg.get_or_create(c)
    assert isinstance(g, rb.CapacityGate)


@pytest.mark.skipif(not rb._HAS_FCNTL, reason="fcntl only")
def test_registry_routes_capacity_machine(tmp_path):
    reg = rb.GateRegistry()
    c = rb.ResourceContract("V", "mem.vram", 1, capacity=8000,
                            enforcement_scope=rb.ENFORCEMENT_MACHINE)
    g = reg.get_or_create(c, lock_dir=tmp_path)
    assert isinstance(g, rb.MachineCapacityGate)


def test_audit_capacity_units():
    # 容量合同: 预算按 capacity 比; demand = 并发 × cost
    cs = [rb.ResourceContract("V", "mem.vram", 1, capacity=8000)]
    fs = [rb.ConcurrencyFactor("mem.vram", "infer", path_count=4, per_path=1, cost=3000)]  # 12000
    reps = rb.audit_concurrency(cs, fs)
    assert reps[0].demanded == 12000 and not reps[0].ok and reps[0].reason == "exceeds_budget"
    fs_ok = [rb.ConcurrencyFactor("mem.vram", "infer", path_count=2, cost=3000)]            # 6000
    assert rb.audit_concurrency(cs, fs_ok)[0].ok


def test_probe_capacity_from_gate():
    gate = rb.CapacityGate(rb.ResourceContract("V", "mem.vram", 1, capacity=8000))
    probe = rb.Probe.from_gate(gate)
    assert probe.current() == 0
    probe.assert_within_budget()
    assert probe._budget == 8000


# ---- MachineCapacityGate (跨进程加权 ledger) ----

@pytest.mark.skipif(not rb._HAS_FCNTL, reason="MachineCapacityGate 需要 fcntl")
def test_machine_capacity_weighted_limit(tmp_path):
    # cap=6000, 每任务 2000 -> 同时最多 3 个 (6000); ledger 跨 OFD 互斥, 等价跨进程
    c = rb.ResourceContract("V", "mem.vram", 1, capacity=6000,
                            enforcement_scope=rb.ENFORCEMENT_MACHINE)
    gate = rb.MachineCapacityGate(c, lock_dir=tmp_path)
    used = {"cur": 0, "max": 0}
    lock = threading.Lock()
    violations = []

    def work():
        try:
            with gate.acquire(cost=2000, timeout=8):
                with lock:
                    used["cur"] += 2000
                    used["max"] = max(used["max"], used["cur"])
                    if used["cur"] > 6000:
                        violations.append(used["cur"])
                time.sleep(0.05)
                with lock:
                    used["cur"] -= 2000
        except Exception as ex:        # noqa: BLE001
            violations.append(repr(ex))

    threads = [threading.Thread(target=work) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads)
    assert not violations, f"跨进程容量越界: {violations}"
    assert used["max"] <= 6000
    assert gate.held_units() == 0       # 全部释放


@pytest.mark.skipif(not rb._HAS_FCNTL, reason="fcntl only")
@pytest.mark.slow   # C3:墙钟型(sleep/TTL),归 pytest-slow 门
def test_machine_capacity_reject_when_full(tmp_path):
    c = rb.ResourceContract("V", "mem.vram", 1, capacity=4000,
                            enforcement_scope=rb.ENFORCEMENT_MACHINE, on_violation=rb.ON_REJECT)
    gate = rb.MachineCapacityGate(c, lock_dir=tmp_path)
    got = {}

    def holder():
        with gate.acquire(cost=3000):
            time.sleep(0.8)

    def late():
        try:
            with gate.acquire(cost=3000, timeout=0.3):     # 6000 > 4000
                pass
        except ZeError as e:
            got["code"] = e.code

    th = threading.Thread(target=holder)
    th.start()
    time.sleep(0.1)
    tl = threading.Thread(target=late)
    tl.start()
    th.join(timeout=10)
    tl.join(timeout=10)
    assert not th.is_alive() and not tl.is_alive()
    assert got.get("code") == "resource_unavailable"


# ---- 序列化 round-trip(P2c-B;manifest / recover 用)----

def test_contract_to_from_dict_roundtrip():
    c = rb.ResourceContract(resource="ESRGAN", scope="gpu.esrgan", max_concurrency=2)
    assert rb.ResourceContract.from_dict(c.to_dict()) == c
    # default 值省略键(紧凑)
    d = c.to_dict()
    assert "enforcement_scope" not in d and "rate_per_sec" not in d and "capacity" not in d
    assert d == {"resource": "ESRGAN", "scope": "gpu.esrgan", "max_concurrency": 2}


def test_contract_to_from_dict_full():
    c = rb.ResourceContract(resource="API", scope="api.openai.chat", max_concurrency=4,
                            enforcement_scope="machine", rate_per_sec=5.0, rate_burst=10.0,
                            on_violation="reject", capacity=None)
    d = c.to_dict()
    assert d["enforcement_scope"] == "machine" and d["rate_per_sec"] == 5.0
    assert d["on_violation"] == "reject"
    assert rb.ResourceContract.from_dict(d) == c


def test_capacity_contract_roundtrip():
    c = rb.ResourceContract(resource="VRAM", scope="mem.vram", max_concurrency=8, capacity=24000)
    d = c.to_dict()
    assert d["capacity"] == 24000
    assert rb.ResourceContract.from_dict(d) == c
