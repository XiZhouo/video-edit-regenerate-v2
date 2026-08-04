"""Resource Budget — 重资源架构合同 + 闸门 + 乘法审计.

完整 SPEC 见同目录 SPEC.md.

核心思想（"架构合同"纪律）：
  任何有限的设备/计算资源（GPU 计算与显存 / 系统内存 / CPU 核 / 磁盘空间与 IO
  带宽 / 网络带宽 / 文件句柄 / DB 连接 / 外部 API …）先写一份合同——
  资源 / 作用域 / 硬上限 / 唯一入口 / 违反时行为 / 验收——再写代码。
  ESRGAN 只是"槽位类"的一个例子，不是模板。
  - 合同 = ResourceContract（frozen dataclass；slot 类填 max_concurrency，capacity 类填 capacity）。
  - 闸门：槽位类 ResourceGate/MachineGate；容量类 CapacityGate/MachineCapacityGate。把合同变运行时强制。
  - audit_concurrency 在发版前用乘法（路径数 × 每路径 × 子并发 × cost）验证总需求不爆预算。
  - 闸门内部运行时自证（在用量 <= 硬上限），不靠"相信代码写对了"。

设备资源约束分三种 kind（完整映射表见 SPEC §1）：
  - 槽位 slot：同时最多 N 个不可分实例 → max_concurrency（Semaphore / machine slot）。
  - 吞吐 throughput：每单位时间最多 R 量 → rate_per_sec + acquire(rate_amount=量)。
  - 容量 capacity：在用量之和 <= 总池（显存/内存/磁盘空间）→ CapacityGate 加权预算
    acquire(cost=本任务用量)，准入 sum(cost) <= capacity（异构权重，非"切等大槽"近似）。
  for_gpu_model / for_cpu_pool / for_external_api（槽位/吞吐）+ for_gpu_vram / for_memory_pool /
  for_capacity_pool（容量）是便捷工厂；其余资源直接用通用构造 ResourceContract(...) 或 from_env。

已实装（M1，无跨叶子依赖）：
  - ResourceContract（slot/capacity 双 kind）+ 6 个环境自适应工厂 + from_env
  - 探测 detect_cpu_count / detect_has_gpu / detect_gpu_vram_mb / detect_total_ram_mb（零重依赖）
  - validate_scope（作用域命名强校验）
  - 槽位类闸门：ResourceGate（进程内 BoundedSemaphore）/ MachineGate（fcntl slot 文件锁）
  - 容量类闸门：CapacityGate（进程内 Condition 加权计数）/ MachineCapacityGate（跨进程 fcntl ledger）
  - 三类闸门都：组合 RateLimiter 速率门 + 运行时自证；GateRegistry 按 (scope,kind) 唯一路由
  - GateRegistry（同 scope 进程内唯一）+ Probe（验收探针，含 pgrep / held_units 计数）
  - audit_concurrency + ConcurrencyFactor（含 cost）+ AuditReport（乘法审计；容量按单位比 capacity）

待补（M2，真有跨叶子依赖）：
  - 乘法审计自动从 03-pipeline 读 fan_out（当前靠 mod 手写声明）
  - 闸门拿不到槽转交 05-concurrency QueueManager 排队报 ETA
  - 多机（跨机器）分布式锁（走 05-concurrency Redis 锁待补；MachineCapacityGate 只管单机）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

# 共享 ZeError + RateLimiter：兄弟叶子注入 sys.path（v3 平铺布局）
_ENTITY_DIR = Path(__file__).resolve().parent.parent.parent / "01-data-structure" / "02-entity"
_CONCURRENCY_DIR = Path(__file__).resolve().parent.parent / "05-concurrency"
for _d in (_ENTITY_DIR, _CONCURRENCY_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from entity import ZeError       # noqa: E402
from rate import RateLimiter     # noqa: E402

# fcntl 仅 Unix 有；MachineGate 跨进程锁依赖它，Windows 优雅降级（构造时报错）
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:              # pragma: no cover (本项目跑在 darwin/linux)
    _HAS_FCNTL = False


# ==============================================================
# 作用域命名（原则 3：配置项名字必须带作用域）
# ==============================================================

# <domain>.<resource>[.<sub>[.<sub>]] —— 2~4 段，全小写，段内字母开头
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*){1,3}$")

# 推荐 domain（第一段）；非推荐不 fail（避免过度约束），SPEC §4 给推荐表
_RECOMMENDED_DOMAINS = frozenset({"gpu", "cpu", "api", "io", "mem"})


def validate_scope(scope: str) -> None:
    """校验作用域命名。非法抛 resource_scope_invalid。

    合法：gpu.esrgan / api.openai.chat / cpu.pool / io.disk.write
    非法：裸名 max_workers / 大写 GPU.x / 单段 gpu / 空串
    """
    if not isinstance(scope, str) or not _SCOPE_RE.match(scope):
        raise ZeError(
            "resource_scope_invalid",
            f"scope {scope!r} 不合法；需 <domain>.<resource>[.<sub>] 全小写点分 2-4 段",
            context={"scope": scope,
                     "expected": "<domain>.<resource>[.<sub>] lowercase dotted 2-4 segments"},
        )


def scope_is_recommended_domain(scope: str) -> bool:
    """第一段是否在推荐 domain 集合内（gpu/cpu/api/io/mem）。仅供 caller 自检，不强制。"""
    return scope.split(".", 1)[0] in _RECOMMENDED_DOMAINS


def _scope_env_prefix(scope: str) -> str:
    """gpu.esrgan -> ZE_RB_GPU_ESRGAN（env 变量前缀）。"""
    return "ZE_RB_" + scope.upper().replace(".", "_")


# ==============================================================
# 环境探测（原则 2：按环境自动判断；零重依赖、绝不抛错、保守降级）
# ==============================================================

def detect_cpu_count() -> int:
    """CPU 逻辑核数。os.cpu_count() 不可用时保守返 1。"""
    try:
        n = os.cpu_count()
    except Exception:
        n = None
    return n if isinstance(n, int) and n > 0 else 1


def detect_has_gpu(env: Mapping[str, str] | None = None) -> bool:
    """零重依赖嗅探有没有（N 卡）GPU。绝不 import torch/pynvml/跑 nvidia-smi 探显存。

    判定顺序（任一命中即定，全 miss 保守当无 GPU）：
      1. ZE_RB_FORCE_GPU = 1/true/yes 或 0/false/no（人工断言，最高）
      2. CUDA_VISIBLE_DEVICES 非空且非 '-1' -> True；空 / '-1' -> False（显式禁用）
      3. Linux /dev/nvidia0 或 /dev/nvidiactl 存在
      4. shutil.which('nvidia-smi') 存在（有驱动）
    """
    src = env if env is not None else os.environ
    forced = src.get("ZE_RB_FORCE_GPU", "").strip().lower()
    if forced in ("1", "true", "yes"):
        return True
    if forced in ("0", "false", "no"):
        return False
    cuda = src.get("CUDA_VISIBLE_DEVICES")
    if cuda is not None:
        c = cuda.strip()
        if c and c != "-1":
            return True
        if c in ("", "-1"):
            return False
    try:
        if os.path.exists("/dev/nvidia0") or os.path.exists("/dev/nvidiactl"):
            return True
    except Exception:
        pass
    try:
        if shutil.which("nvidia-smi") is not None:
            return True
    except Exception:
        pass
    return False


def detect_gpu_vram_mb(env: Mapping[str, str] | None = None) -> int | None:
    """GPU 显存 MB。默认 None（不探显存，避免重依赖）；仅当 ZE_RB_GPU_VRAM_MB 显式给值才返。

    被 ResourceContract.for_gpu_vram() 消费作容量池总量（缺省保守 8192）；CapacityGate 据此
    加权准入 sum(cost) <= 总显存。
    """
    src = env if env is not None else os.environ
    raw = src.get("ZE_RB_GPU_VRAM_MB")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def detect_total_ram_mb() -> int | None:
    """系统总内存 MB。零重依赖：os.sysconf（Linux/macOS 都有）→ 回退读 /proc/meminfo；
    都拿不到返 None（绝不引 psutil）。供容量类工厂算系统内存预算用。"""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if isinstance(pages, int) and isinstance(page_size, int) and pages > 0 and page_size > 0:
            return (pages * page_size) // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024     # kB → MB
    except (OSError, ValueError, IndexError):
        pass
    return None


# ---- env 读取 helper（非法值抛 resource_budget_invalid，对齐 capacity from_env）----

def _env_int(env: Mapping[str, str] | None, name: str, default: int | None) -> int | None:
    src = env if env is not None else os.environ
    raw = src.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ZeError("resource_budget_invalid", f"{name}={raw!r} 不是整数",
                      context={"env": name, "value": raw})
    if v < 1:
        raise ZeError("resource_budget_invalid", f"{name}={v} 必须 >= 1",
                      context={"env": name, "value": v})
    return v


def _env_float(env: Mapping[str, str] | None, name: str, default: float | None) -> float | None:
    src = env if env is not None else os.environ
    raw = src.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ZeError("resource_budget_invalid", f"{name}={raw!r} 不是数字",
                      context={"env": name, "value": raw})
    if v <= 0:
        raise ZeError("resource_budget_invalid", f"{name}={v} 必须 > 0",
                      context={"env": name, "value": v})
    return v


# ==============================================================
# ResourceContract（架构合同的代码化身）
# ==============================================================

# enforcement_scope：硬上限管多大范围
ENFORCEMENT_PROCESS = "process"     # 单进程内多线程
ENFORCEMENT_MACHINE = "machine"     # 单台机器全局（多进程）
_ENFORCEMENT_SCOPES = frozenset({ENFORCEMENT_PROCESS, ENFORCEMENT_MACHINE})

# on_violation：拿不到资源时的行为
ON_WAIT = "wait"                    # 等待（不丢任务/不额外启动）—— ESRGAN 式
ON_REJECT = "reject"                # 立即拒绝
ON_CANCEL_RETRY = "cancel_retry"    # 取消未完成，由 caller retry 重新申请 —— API 式
_ON_VIOLATIONS = frozenset({ON_WAIT, ON_REJECT, ON_CANCEL_RETRY})


@dataclass(frozen=True)
class ResourceContract:
    """一个重资源的架构合同。frozen → 不可中途改、hash 稳定。

    字段对齐"架构合同"6 要素：
      resource          — 资源（人类可读名，如 "ESRGAN"）
      enforcement_scope — 作用域（硬上限管多大：process / machine）
      scope             — 命名 key，带作用域（原则 3），如 "gpu.esrgan"
      max_concurrency   — 硬上限（槽位数；容量类填最坏并发数，展示用）
      capacity          — 硬上限（容量单位池，如显存/内存 MB；给值即"容量类"，闸门按加权 cost 准入）
      rate_per_sec      — 硬上限（速率，可选；外部 API / 带宽用）
      on_violation      — 违反时行为（wait / reject / cancel_retry）
    "唯一入口" = ResourceGate / CapacityGate.acquire()；"验收" = Probe + 并发边界测试（机制，非字段）。

    kind（见 .kind 属性）：capacity 给值 → "容量"（CapacityGate，加权 sum(cost)≤capacity）；
    否则 → "槽位"（ResourceGate，count≤max_concurrency）。throughput 由 rate_per_sec 正交叠加。
    """

    resource: str
    scope: str
    max_concurrency: int
    enforcement_scope: str = ENFORCEMENT_PROCESS
    rate_per_sec: float | None = None
    rate_burst: float | None = None
    on_violation: str = ON_WAIT
    capacity: int | None = None

    @property
    def kind(self) -> str:
        """槽位 slot（capacity=None）/ 容量 capacity（capacity 给值）。"""
        return "capacity" if self.capacity is not None else "slot"

    def __post_init__(self) -> None:
        validate_scope(self.scope)
        if not isinstance(self.max_concurrency, int) or isinstance(self.max_concurrency, bool) \
                or self.max_concurrency < 1:
            raise ZeError("resource_budget_invalid",
                          f"max_concurrency 必须是 >= 1 的整数，got {self.max_concurrency!r}",
                          context={"scope": self.scope, "max_concurrency": self.max_concurrency})
        if self.enforcement_scope not in _ENFORCEMENT_SCOPES:
            raise ZeError("resource_budget_invalid",
                          f"enforcement_scope 必须在 {sorted(_ENFORCEMENT_SCOPES)}",
                          context={"scope": self.scope, "enforcement_scope": self.enforcement_scope})
        if self.on_violation not in _ON_VIOLATIONS:
            raise ZeError("resource_budget_invalid",
                          f"on_violation 必须在 {sorted(_ON_VIOLATIONS)}",
                          context={"scope": self.scope, "on_violation": self.on_violation})
        if self.rate_per_sec is not None and self.rate_per_sec <= 0:
            raise ZeError("resource_budget_invalid",
                          f"rate_per_sec 若给必 > 0，got {self.rate_per_sec}",
                          context={"scope": self.scope, "rate_per_sec": self.rate_per_sec})
        if self.rate_burst is not None and self.rate_burst <= 0:
            raise ZeError("resource_budget_invalid",
                          f"rate_burst 若给必 > 0，got {self.rate_burst}",
                          context={"scope": self.scope, "rate_burst": self.rate_burst})
        if self.capacity is not None:
            if not isinstance(self.capacity, int) or isinstance(self.capacity, bool) \
                    or self.capacity < 1:
                raise ZeError("resource_budget_invalid",
                              f"capacity 若给必是 >= 1 的整数，got {self.capacity!r}",
                              context={"scope": self.scope, "capacity": self.capacity})

    # ---- 序列化（manifest round-trip / recover 用；纯值对象,无探测）----

    def to_dict(self) -> dict[str, Any]:
        """可序列化合同(default 值省略键,紧凑 + 向后兼容)。"""
        d: dict[str, Any] = {
            "resource": self.resource,
            "scope": self.scope,
            "max_concurrency": self.max_concurrency,
        }
        if self.enforcement_scope != ENFORCEMENT_PROCESS:
            d["enforcement_scope"] = self.enforcement_scope
        if self.rate_per_sec is not None:
            d["rate_per_sec"] = self.rate_per_sec
        if self.rate_burst is not None:
            d["rate_burst"] = self.rate_burst
        if self.on_violation != ON_WAIT:
            d["on_violation"] = self.on_violation
        if self.capacity is not None:
            d["capacity"] = self.capacity
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ResourceContract":
        return cls(
            resource=d["resource"],
            scope=d["scope"],
            max_concurrency=int(d["max_concurrency"]),
            enforcement_scope=d.get("enforcement_scope", ENFORCEMENT_PROCESS),
            rate_per_sec=d.get("rate_per_sec"),
            rate_burst=d.get("rate_burst"),
            on_violation=d.get("on_violation", ON_WAIT),
            capacity=d.get("capacity"),
        )

    # ---- 环境自适应工厂（探测放工厂，dataclass 保持纯值对象）----

    @classmethod
    def for_gpu_model(cls, scope: str, *, resource: str = "",
                      env: Mapping[str, str] | None = None,
                      on_violation: str = ON_WAIT,
                      enforcement_scope: str = ENFORCEMENT_MACHINE) -> "ResourceContract":
        """GPU 重模型（ESRGAN/超分）：默认 max_concurrency=1（单 GPU 同时跑多个重模型必爆显存）。
        有无 GPU 都用 1（CPU 回退也安全）。env ZE_RB_<SCOPE>_MAX_CONCURRENCY 可覆盖。
        默认 enforcement_scope=machine（重模型常是独立子进程，单机全局才挡得住）。"""
        prefix = _scope_env_prefix(scope)
        max_c = _env_int(env, prefix + "_MAX_CONCURRENCY", 1)
        rate = _env_float(env, prefix + "_RATE_PER_SEC", None)
        return cls(resource=resource or scope, scope=scope, max_concurrency=max_c,
                   enforcement_scope=enforcement_scope, rate_per_sec=rate,
                   on_violation=on_violation)

    @classmethod
    def for_cpu_pool(cls, scope: str, *, resource: str = "",
                     env: Mapping[str, str] | None = None, reserve: int = 1,
                     on_violation: str = ON_WAIT) -> "ResourceContract":
        """CPU worker 池：默认 max(1, cpu_count - reserve)，留核给主线程/IO。env 可覆盖。"""
        base = max(1, detect_cpu_count() - max(0, reserve))
        max_c = _env_int(env, _scope_env_prefix(scope) + "_MAX_CONCURRENCY", base)
        return cls(resource=resource or scope, scope=scope, max_concurrency=max_c,
                   enforcement_scope=ENFORCEMENT_PROCESS, on_violation=on_violation)

    @classmethod
    def for_external_api(cls, scope: str, *, rate_per_sec: float, max_concurrency: int,
                         resource: str = "", env: Mapping[str, str] | None = None,
                         rate_burst: float | None = None,
                         on_violation: str = ON_CANCEL_RETRY,
                         enforcement_scope: str = ENFORCEMENT_PROCESS) -> "ResourceContract":
        """外部 API：速率/并发天花板来自 API 文档（不探测）；env 只能下调，不可上调超过给定值。"""
        prefix = _scope_env_prefix(scope)
        rate = _env_float(env, prefix + "_RATE_PER_SEC", rate_per_sec)
        rate = min(rate, rate_per_sec)              # env 只能下调速率
        max_c = _env_int(env, prefix + "_MAX_CONCURRENCY", max_concurrency)
        max_c = min(max_c, max_concurrency)         # env 只能下调并发
        return cls(resource=resource or scope, scope=scope, max_concurrency=max_c,
                   enforcement_scope=enforcement_scope, rate_per_sec=rate,
                   rate_burst=rate_burst, on_violation=on_violation)

    @classmethod
    def from_env(cls, *, defaults: "ResourceContract",
                 env: Mapping[str, str] | None = None) -> "ResourceContract":
        """通用：给一个探测好的 defaults，env 逐字段覆盖。覆盖优先级见 SPEC §8。"""
        prefix = _scope_env_prefix(defaults.scope)
        max_c = _env_int(env, prefix + "_MAX_CONCURRENCY", defaults.max_concurrency)
        rate = _env_float(env, prefix + "_RATE_PER_SEC", defaults.rate_per_sec)
        cap = defaults.capacity
        if cap is not None:
            cap = _env_int(env, prefix + "_CAPACITY", cap)
            cap = min(cap, defaults.capacity)        # env 只能下调容量
        return cls(resource=defaults.resource, scope=defaults.scope, max_concurrency=max_c,
                   enforcement_scope=defaults.enforcement_scope, rate_per_sec=rate,
                   rate_burst=defaults.rate_burst, on_violation=defaults.on_violation, capacity=cap)

    # ---- 容量类工厂（显存 / 内存 / 磁盘空间：加权池，acquire(cost=本任务用量)）----

    @classmethod
    def for_capacity_pool(cls, scope: str, *, capacity: int, resource: str = "",
                          per_job: int | None = None, env: Mapping[str, str] | None = None,
                          enforcement_scope: str = ENFORCEMENT_MACHINE,
                          on_violation: str = ON_WAIT,
                          rate_per_sec: float | None = None) -> "ResourceContract":
        """通用容量池（显存 / 内存 / 磁盘空间）：budget=capacity 单位，acquire(cost=本任务用量)。
        env ZE_RB_<SCOPE>_CAPACITY 可下调（min）。per_job 给定时推 max_concurrency=最坏并发数（展示）。
        默认 machine 级（整卡显存 / 系统内存被同机所有进程共享，跨进程才挡得住）。"""
        prefix = _scope_env_prefix(scope)
        cap = _env_int(env, prefix + "_CAPACITY", capacity)
        cap = min(cap, capacity)                     # env 只能下调
        rate = _env_float(env, prefix + "_RATE_PER_SEC", rate_per_sec)
        max_c = max(1, cap // per_job) if per_job else cap
        return cls(resource=resource or scope, scope=scope, max_concurrency=max_c, capacity=cap,
                   enforcement_scope=enforcement_scope, rate_per_sec=rate, on_violation=on_violation)

    @classmethod
    def for_gpu_vram(cls, scope: str, *, per_job_mb: int, total_mb: int | None = None,
                     resource: str = "GPU-VRAM", env: Mapping[str, str] | None = None,
                     on_violation: str = ON_WAIT) -> "ResourceContract":
        """GPU 显存容量。total 来自 detect_gpu_vram_mb()/env ZE_RB_GPU_VRAM_MB（缺省保守 8192）。
        machine 级（整卡显存被同机所有进程共享）。acquire(cost=本任务峰值显存 MB)。"""
        total = total_mb if total_mb is not None else (detect_gpu_vram_mb(env) or 8192)
        return cls.for_capacity_pool(scope, capacity=total, per_job=per_job_mb, resource=resource,
                                     env=env, enforcement_scope=ENFORCEMENT_MACHINE,
                                     on_violation=on_violation)

    @classmethod
    def for_memory_pool(cls, scope: str, *, per_job_mb: int | None = None,
                        total_mb: int | None = None, reserve_mb: int = 512,
                        resource: str = "system-memory", env: Mapping[str, str] | None = None,
                        on_violation: str = ON_WAIT,
                        enforcement_scope: str = ENFORCEMENT_MACHINE) -> "ResourceContract":
        """系统内存容量。total 来自 detect_total_ram_mb()（留 reserve_mb 给系统；探测失败保守 4096）。
        acquire(cost=本任务峰值 RSS MB)。"""
        total = total_mb if total_mb is not None else detect_total_ram_mb()
        if total is None:
            total = 4096
        usable = max(1, total - max(0, reserve_mb))
        return cls.for_capacity_pool(scope, capacity=usable, per_job=per_job_mb, resource=resource,
                                     env=env, enforcement_scope=enforcement_scope,
                                     on_violation=on_violation)


# ==============================================================
# ResourceGate — 进程内唯一入口闸门（原则 1 + 原则 5 运行时自证）
# ==============================================================

def _eff_timeout(contract: ResourceContract, timeout: float | None) -> float | None:
    """根据 on_violation 推有效等待超时。
       timeout 显式给 → 用它；timeout=None 且 on_violation!=wait → 0（不等）；否则 None（无限等）。"""
    if timeout is not None:
        return timeout
    if contract.on_violation != ON_WAIT:
        return 0.0
    return None


class ResourceGate:
    """单个重资源的唯一入口（进程内）。线程安全。两道串联门：
       速率门（组合 05-concurrency RateLimiter）+ 并发门（BoundedSemaphore）。

    用法：
        gate = ResourceGate(contract)
        with gate.acquire():
            call_heavy_resource()    # 真正调资源的代码放 with 体内
    """

    def __init__(self, contract: ResourceContract, *, rate_limiter: RateLimiter | None = None):
        self._contract = contract
        self._sem = threading.BoundedSemaphore(contract.max_concurrency)
        self._active = 0
        self._active_lock = threading.Lock()
        self._peak = 0
        if contract.rate_per_sec is not None:
            self._rl: RateLimiter | None = rate_limiter or RateLimiter()
            self._rl.configure(contract.scope, rate=contract.rate_per_sec,
                               capacity=contract.rate_burst or contract.rate_per_sec)
        else:
            self._rl = None

    @property
    def contract(self) -> ResourceContract:
        return self._contract

    @contextmanager
    def acquire(self, *, timeout: float | None = None, rate_amount: float = 1.0) -> Iterator[None]:
        """唯一入口（槽位类）。先过速率门，再拿 1 个并发槽；with 体退出自动释放（含异常）。

        rate_amount 只作用于速率门（吞吐按量，如带宽 n=字节数）；并发门恒占 1 槽（槽位类语义）。
        容量加权（一个任务占 N 份）走 CapacityGate.acquire(cost=)，不是这里。
        拿不到时：按 on_violation（wait 无限等 / reject / cancel_retry 不等）+ 显式 timeout 控制；
        超时抛 ZeError('resource_unavailable')。进体前在临界区做运行时自证。
        """
        et = _eff_timeout(self._contract, timeout)

        # 门 1：速率
        if self._rl is not None:
            if not self._rl.wait_for(self._contract.scope, rate_amount, timeout=et):
                raise ZeError("resource_unavailable",
                              f"rate limited on {self._contract.scope}",
                              context={"scope": self._contract.scope, "door": "rate"})

        # 门 2：并发槽
        if et is None:
            acquired = self._sem.acquire()
        else:
            acquired = self._sem.acquire(timeout=et)
        if not acquired:
            raise ZeError("resource_unavailable",
                          f"no concurrency slot on {self._contract.scope}",
                          context={"scope": self._contract.scope, "door": "concurrency",
                                   "max_concurrency": self._contract.max_concurrency})

        # 运行时自证：active 不得超预算（理论永不触发；触发=闸门被错配，如 semaphore 容量被改）
        with self._active_lock:
            self._active += 1
            if self._active > self._contract.max_concurrency:
                cur = self._active
                self._active -= 1
                self._sem.release()
                raise ZeError("resource_budget_exceeded",
                              f"active {cur} > budget {self._contract.max_concurrency} "
                              f"on {self._contract.scope}",
                              context={"scope": self._contract.scope, "active": cur,
                                       "budget": self._contract.max_concurrency})
            if self._active > self._peak:
                self._peak = self._active

        try:
            yield
        finally:
            with self._active_lock:
                self._active -= 1
            self._sem.release()

    def stats(self) -> dict[str, Any]:
        """诊断（不抛）：{scope, max_concurrency, active, peak, available, enforcement_scope}。"""
        with self._active_lock:
            active = self._active
            peak = self._peak
        return {"scope": self._contract.scope,
                "max_concurrency": self._contract.max_concurrency,
                "active": active, "peak": peak,
                "available": max(0, self._contract.max_concurrency - active),
                "enforcement_scope": self._contract.enforcement_scope}


# ==============================================================
# CapacityGate — 进程内加权容量闸门（容量类：显存/内存/磁盘空间）
# ==============================================================

class CapacityGate:
    """单个容量类资源的唯一入口（进程内，加权）。线程安全。

    与 ResourceGate（槽位，恒占 1）的区别：acquire(cost=本任务用量) 从 capacity 单位池里扣
    cost，准入条件 sum(已占) + cost <= capacity——异构权重之和受限（A 吃 3000、B 吃 5000，
    8000 池里 A+B 不行、A+A 行）。这才是显存/内存的真实约束，不是"切等大槽"近似。

    用法：
        gate = CapacityGate(ResourceContract.for_gpu_vram("mem.vram", per_job_mb=3000))
        with gate.acquire(cost=3000):
            run_inference()         # 同时在跑的任务峰值显存之和 <= 总显存
    """

    def __init__(self, contract: ResourceContract, *, rate_limiter: RateLimiter | None = None):
        if contract.capacity is None:
            raise ZeError("resource_budget_invalid",
                          f"CapacityGate 需要 capacity（容量类）；{contract.scope!r} 没给 capacity",
                          context={"scope": contract.scope})
        self._contract = contract
        self._budget = contract.capacity
        self._available = contract.capacity
        self._in_use = 0
        self._peak = 0
        self._cond = threading.Condition()
        if contract.rate_per_sec is not None:
            self._rl: RateLimiter | None = rate_limiter or RateLimiter()
            self._rl.configure(contract.scope, rate=contract.rate_per_sec,
                               capacity=contract.rate_burst or contract.rate_per_sec)
        else:
            self._rl = None

    @property
    def contract(self) -> ResourceContract:
        return self._contract

    @contextmanager
    def acquire(self, *, cost: int, timeout: float | None = None,
                rate_amount: float = 1.0) -> Iterator[None]:
        """唯一入口（容量类）。cost = 本任务用量（如峰值显存 MB）。

        cost > capacity 立即抛 resource_budget_invalid（单任务永远装不下，等也没用）。
        rate_amount 只作用于速率门（容量类通常无 rate）。退出自动归还 cost。
        """
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost <= 0:
            raise ZeError("resource_budget_invalid",
                          f"cost 必须 > 0，got {cost!r}",
                          context={"scope": self._contract.scope, "cost": cost})
        if cost > self._budget:
            raise ZeError("resource_budget_invalid",
                          f"cost {cost} > capacity {self._budget} on {self._contract.scope}"
                          f"（单任务超过总容量，永远装不下）",
                          context={"scope": self._contract.scope, "cost": cost,
                                   "capacity": self._budget})
        et = _eff_timeout(self._contract, timeout)

        # 门 1：速率（可选）
        if self._rl is not None:
            if not self._rl.wait_for(self._contract.scope, rate_amount, timeout=et):
                raise ZeError("resource_unavailable",
                              f"rate limited on {self._contract.scope}",
                              context={"scope": self._contract.scope, "door": "rate"})

        # 门 2：加权容量（Condition 计数）
        with self._cond:
            if not self._cond.wait_for(lambda: self._available >= cost, timeout=et):
                raise ZeError("resource_unavailable",
                              f"no capacity on {self._contract.scope}"
                              f"（need {cost}, available {self._available}/{self._budget}）",
                              context={"scope": self._contract.scope, "door": "capacity",
                                       "cost": cost, "available": self._available,
                                       "capacity": self._budget})
            self._available -= cost
            self._in_use += cost
            # 运行时自证：在用量不得超容量（理论永不触发；触发=闸门被错配）
            if self._in_use > self._budget:
                cur = self._in_use
                self._available += cost
                self._in_use -= cost
                self._cond.notify_all()
                raise ZeError("resource_budget_exceeded",
                              f"in_use {cur} > capacity {self._budget} on {self._contract.scope}",
                              context={"scope": self._contract.scope, "in_use": cur,
                                       "capacity": self._budget})
            if self._in_use > self._peak:
                self._peak = self._in_use

        try:
            yield
        finally:
            with self._cond:
                self._available += cost
                self._in_use -= cost
                self._cond.notify_all()

    def stats(self) -> dict[str, Any]:
        with self._cond:
            in_use = self._in_use
            peak = self._peak
            available = self._available
        return {"scope": self._contract.scope, "capacity": self._budget,
                "max_concurrency": self._contract.max_concurrency,
                "active": in_use, "peak": peak, "available": max(0, available),
                "enforcement_scope": self._contract.enforcement_scope}


# ==============================================================
# MachineGate — 单机跨进程闸门（fcntl slot 文件锁；对应"作用域:单台机器全局"）
# ==============================================================

class MachineGate:
    """单台机器全局闸门（跨进程）。后端：lock_dir 下 N 个 slot 文件 + fcntl 排他锁。
       对应"作用域:单台机器全局"的合同（如 ESRGAN/upscayl-bin 多进程）。

    机制：N = max_concurrency 个 slot；acquire = 抢占任一空 slot 的 flock；
    占满则按 on_violation 等待/拒绝。跨进程：所有进程走同一 lock_dir，
    被锁 slot 数 <= N 由 OS 文件锁物理保证（这本身就是 machine 级的"自证"）。
    """

    def __init__(self, contract: ResourceContract, *, lock_dir: str | Path | None = None,
                 rate_limiter: RateLimiter | None = None, poll_interval: float = 0.05):
        if not _HAS_FCNTL:
            raise ZeError("resource_budget_invalid",
                          "MachineGate 需要 fcntl（Unix only）；本平台不支持 machine 级闸门",
                          context={"scope": contract.scope})
        self._contract = contract
        self._n = contract.max_concurrency
        if lock_dir is not None:
            base = Path(lock_dir)
        else:
            root = Path(os.environ.get("ZE_RB_LOCK_DIR", "/tmp/ze_rb"))
            base = root / contract.scope.replace(".", "_")
        base.mkdir(parents=True, exist_ok=True)
        self._lock_dir = base
        self._poll = poll_interval
        if contract.rate_per_sec is not None:
            self._rl: RateLimiter | None = rate_limiter or RateLimiter()
            self._rl.configure(contract.scope, rate=contract.rate_per_sec,
                               capacity=contract.rate_burst or contract.rate_per_sec)
        else:
            self._rl = None

    @property
    def contract(self) -> ResourceContract:
        return self._contract

    def _slot_path(self, i: int) -> Path:
        return self._lock_dir / f"slot_{i}.lock"

    @contextmanager
    def acquire(self, *, timeout: float | None = None, rate_amount: float = 1.0) -> Iterator[None]:
        """唯一入口（跨进程，槽位类）。先过速率门，再抢一个 slot 文件锁；with 体退出自动释放。
        rate_amount 只作用于速率门；slot 恒占 1。容量加权走 MachineCapacityGate。"""
        et = _eff_timeout(self._contract, timeout)

        # 门 1：速率
        if self._rl is not None:
            if not self._rl.wait_for(self._contract.scope, rate_amount, timeout=et):
                raise ZeError("resource_unavailable",
                              f"rate limited on {self._contract.scope}",
                              context={"scope": self._contract.scope, "door": "rate"})

        # 门 2：抢 slot 文件锁（跨进程）
        deadline = (time.time() + et) if et is not None else None
        fd = None
        while True:
            for i in range(self._n):
                f = open(self._slot_path(i), "a+")
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fd = f
                    break
                except (OSError, BlockingIOError):
                    f.close()
            if fd is not None:
                break
            if deadline is not None and time.time() >= deadline:
                raise ZeError("resource_unavailable",
                              f"no machine slot on {self._contract.scope}（all {self._n} held）",
                              context={"scope": self._contract.scope, "door": "machine_slot",
                                       "max_concurrency": self._n})
            time.sleep(self._poll)

        try:
            yield
        finally:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            finally:
                fd.close()

    def held_count(self) -> int:
        """当前被占用的 slot 数（跨进程探针，验收用）。非阻塞试锁每个 slot，锁不上的即被占用。"""
        held = 0
        for i in range(self._n):
            f = open(self._slot_path(i), "a+")
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)      # 能锁上 = 空闲
            except (OSError, BlockingIOError):
                held += 1
            finally:
                f.close()
        return held

    def stats(self) -> dict[str, Any]:
        held = self.held_count()
        return {"scope": self._contract.scope, "max_concurrency": self._n,
                "active": held, "available": max(0, self._n - held),
                "enforcement_scope": ENFORCEMENT_MACHINE, "lock_dir": str(self._lock_dir)}


# ==============================================================
# MachineCapacityGate — 单机跨进程加权容量闸门（fcntl-locked ledger）
# ==============================================================

class MachineCapacityGate:
    """单台机器全局的容量类闸门（跨进程，加权）。对应"作用域:单台机器全局 + 容量类"，
    如整卡 GPU 显存 / 系统内存被同机多个独立进程共享。

    机制：一个 fcntl 排他锁保护的 ledger.json（[{token,pid,cost,ts}]）。acquire(cost)：
    锁 ledger → 清死进程残留 → 算在用量之和 → 若 sum+cost<=capacity 则追加自己的条目、解锁、进入；
    否则解锁、轮询重试。跨进程：所有进程走同一 ledger，sum<=capacity 由文件锁下的读改写物理保证。
    死进程残留靠 os.kill(pid,0) 存活检查在每次 acquire/held_units 时清理。
    """

    def __init__(self, contract: ResourceContract, *, lock_dir: str | Path | None = None,
                 rate_limiter: RateLimiter | None = None, poll_interval: float = 0.05):
        if not _HAS_FCNTL:
            raise ZeError("resource_budget_invalid",
                          "MachineCapacityGate 需要 fcntl（Unix only）；本平台不支持 machine 级闸门",
                          context={"scope": contract.scope})
        if contract.capacity is None:
            raise ZeError("resource_budget_invalid",
                          f"MachineCapacityGate 需要 capacity；{contract.scope!r} 没给",
                          context={"scope": contract.scope})
        self._contract = contract
        self._budget = contract.capacity
        if lock_dir is not None:
            base = Path(lock_dir)
        else:
            root = Path(os.environ.get("ZE_RB_LOCK_DIR", "/tmp/ze_rb"))
            base = root / contract.scope.replace(".", "_")
        base.mkdir(parents=True, exist_ok=True)
        self._dir = base
        self._ledger = base / "ledger.json"
        self._lockfile = base / "ledger.lock"
        self._poll = poll_interval
        if contract.rate_per_sec is not None:
            self._rl: RateLimiter | None = rate_limiter or RateLimiter()
            self._rl.configure(contract.scope, rate=contract.rate_per_sec,
                               capacity=contract.rate_burst or contract.rate_per_sec)
        else:
            self._rl = None

    @property
    def contract(self) -> ResourceContract:
        return self._contract

    @contextmanager
    def _ledger_lock(self) -> Iterator[None]:
        f = open(self._lockfile, "a+")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)      # 阻塞短临界区
            yield
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            finally:
                f.close()

    @staticmethod
    def _pid_alive(pid: Any) -> bool:
        try:
            pid_i = int(pid)
        except (TypeError, ValueError):
            return False                                # 坏条目当死，会被清理
        try:
            os.kill(pid_i, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True                                 # 存在但属别的用户
        except OSError:
            return True
        return True

    def _load(self) -> list[dict[str, Any]]:
        """读 ledger（清死进程残留）。须在 _ledger_lock 内调。"""
        try:
            with open(self._ledger) as f:
                entries = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return []
        if not isinstance(entries, list):
            return []
        live = [e for e in entries if isinstance(e, dict) and self._pid_alive(e.get("pid", -1))]
        if len(live) != len(entries):
            self._save(live)                            # 顺手回写清理后的
        return live

    def _save(self, entries: list[dict[str, Any]]) -> None:
        """原子写 ledger。须在 _ledger_lock 内调。"""
        tmp = self._ledger.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(entries, f)
        os.replace(tmp, self._ledger)

    @contextmanager
    def acquire(self, *, cost: int, timeout: float | None = None,
                rate_amount: float = 1.0) -> Iterator[None]:
        """唯一入口（跨进程，容量类）。cost = 本任务用量。cost>capacity 立即抛 resource_budget_invalid。"""
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost <= 0:
            raise ZeError("resource_budget_invalid", f"cost 必须 > 0，got {cost!r}",
                          context={"scope": self._contract.scope, "cost": cost})
        if cost > self._budget:
            raise ZeError("resource_budget_invalid",
                          f"cost {cost} > capacity {self._budget} on {self._contract.scope}"
                          f"（单任务超过总容量，永远装不下）",
                          context={"scope": self._contract.scope, "cost": cost,
                                   "capacity": self._budget})
        et = _eff_timeout(self._contract, timeout)

        # 门 1：速率（可选）
        if self._rl is not None:
            if not self._rl.wait_for(self._contract.scope, rate_amount, timeout=et):
                raise ZeError("resource_unavailable", f"rate limited on {self._contract.scope}",
                              context={"scope": self._contract.scope, "door": "rate"})

        # 门 2：加权容量（ledger 读改写）
        token = uuid.uuid4().hex
        deadline = (time.time() + et) if et is not None else None
        while True:
            with self._ledger_lock():
                entries = self._load()
                used = sum(int(e.get("cost", 0)) for e in entries)
                if used + cost <= self._budget:
                    entries.append({"token": token, "pid": os.getpid(),
                                    "cost": cost, "ts": time.time()})
                    self._save(entries)
                    acquired = True
                else:
                    acquired = False
            if acquired:
                break
            if deadline is not None and time.time() >= deadline:
                raise ZeError("resource_unavailable",
                              f"no machine capacity on {self._contract.scope}"
                              f"（need {cost}, capacity {self._budget}）",
                              context={"scope": self._contract.scope, "door": "machine_capacity",
                                       "cost": cost, "capacity": self._budget})
            time.sleep(self._poll)

        try:
            yield
        finally:
            with self._ledger_lock():
                entries = [e for e in self._load() if e.get("token") != token]
                self._save(entries)

    def held_units(self) -> int:
        """当前在用容量单位之和（跨进程探针，验收用）。"""
        with self._ledger_lock():
            return sum(int(e.get("cost", 0)) for e in self._load())

    def stats(self) -> dict[str, Any]:
        held = self.held_units()
        return {"scope": self._contract.scope, "capacity": self._budget,
                "max_concurrency": self._contract.max_concurrency,
                "active": held, "available": max(0, self._budget - held),
                "enforcement_scope": ENFORCEMENT_MACHINE, "lock_dir": str(self._dir)}


# ==============================================================
# GateRegistry — 同 scope 进程内唯一（原则 1：唯一入口）
# ==============================================================

class GateRegistry:
    """进程内闸门登记处。同 scope 只能注册一个闸门，防"每层各建一个 semaphore"。

    用法（framework 启动时建单例）：
        registry = GateRegistry()
        gate = registry.get_or_create(contract)   # 按 enforcement_scope 自动选 Resource/Machine
        with registry.get("gpu.esrgan").acquire():
            ...
    """

    def __init__(self) -> None:
        self._gates: dict[str, Any] = {}
        self._lock = threading.Lock()

    def register(self, gate: Any) -> Any:
        scope = gate.contract.scope
        with self._lock:
            if scope in self._gates:
                raise ZeError("resource_scope_duplicate",
                              f"scope {scope!r} 已注册闸门；同 scope 只能有一个唯一入口",
                              context={"scope": scope})
            self._gates[scope] = gate
        return gate

    def get(self, scope: str) -> Any:
        with self._lock:
            gate = self._gates.get(scope)
        if gate is None:
            raise ZeError("resource_gate_not_found",
                          f"scope {scope!r} 没有注册闸门；先 register / get_or_create",
                          context={"scope": scope})
        return gate

    def get_or_create(self, contract: ResourceContract, **kwargs: Any) -> Any:
        """按 (enforcement_scope, kind) 四向路由：
           process+slot → ResourceGate；process+capacity → CapacityGate；
           machine+slot → MachineGate；machine+capacity → MachineCapacityGate。"""
        with self._lock:
            existing = self._gates.get(contract.scope)
            if existing is not None:
                return existing
            machine = contract.enforcement_scope == ENFORCEMENT_MACHINE
            if contract.kind == "capacity":
                gate: Any = (MachineCapacityGate(contract, **kwargs) if machine
                             else CapacityGate(contract, **kwargs))
            else:
                gate = MachineGate(contract, **kwargs) if machine else ResourceGate(contract, **kwargs)
            self._gates[contract.scope] = gate
            return gate

    def scopes(self) -> list[str]:
        with self._lock:
            return sorted(self._gates)


# ==============================================================
# Probe — 验收探针（合同"验收"字段：运行时实测真实占用）
# ==============================================================

class Probe:
    """验收探针：实测某资源的真实并发占用，跟硬上限比。对应合同"验收"字段。

    用于压力测试断言"真实占用 <= 硬上限"（如 ESRGAN：pgrep upscayl-bin <= 3）。
    """

    def __init__(self, scope: str, budget: int, *, count_fn: Callable[[], int]) -> None:
        self._scope = scope
        self._budget = budget
        self._count_fn = count_fn

    def current(self) -> int:
        return self._count_fn()

    def assert_within_budget(self) -> None:
        cur = self._count_fn()
        if cur > self._budget:
            raise ZeError("resource_budget_exceeded",
                          f"probe on {self._scope}: 实测 {cur} > 预算 {self._budget}",
                          context={"scope": self._scope, "actual": cur, "budget": self._budget})

    @classmethod
    def from_gate(cls, gate: Any) -> "Probe":
        budget = gate.contract.capacity if gate.contract.capacity is not None \
            else gate.contract.max_concurrency
        return cls(gate.contract.scope, budget,
                   count_fn=lambda: gate.stats()["active"])

    @staticmethod
    def pgrep_count(pattern: str) -> int:
        """数匹配 pattern 的进程数（验收 ESRGAN/upscayl-bin 式资源）。无 pgrep 返 0。"""
        if shutil.which("pgrep") is None:
            return 0
        import subprocess
        try:
            out = subprocess.run(["pgrep", "-f", pattern],
                                 capture_output=True, text=True, timeout=5)
            return len([ln for ln in out.stdout.splitlines() if ln.strip()])
        except Exception:
            return 0


# ==============================================================
# 乘法审计（原则 4：总并发 = 路径数 × 每路径 × 子并发，跟预算比）
# ==============================================================

@dataclass(frozen=True)
class ConcurrencyFactor:
    """一条调用路径对某 scope 的并发声明。真实贡献 = path_count * per_path * sub * cost。

    槽位类：cost=1（默认），demand = 并发个数，跟 max_concurrency 比。
    容量类：cost = 单任务用量（如峰值显存 MB），demand = 并发个数 × 单任务用量，跟 capacity 比。
    """
    scope: str
    path_label: str
    path_count: int = 1     # 这条路径有几个并发实例（如几个 worker）
    per_path: int = 1       # 每实例内并发（如 pipeline fan_out N）
    sub: int = 1            # 每任务再子并发（如每张图 2 个 tile）
    cost: int = 1           # 每并发占的资源单位（槽位类=1；容量类=单任务用量，如显存 MB）

    def demand(self) -> int:
        return self.path_count * self.per_path * self.sub * self.cost


@dataclass(frozen=True)
class AuditReport:
    scope: str
    budget: int | None      # None = 该 scope 没声明合同
    demanded: int
    factors: tuple[ConcurrencyFactor, ...]
    ok: bool
    reason: str = ""        # "" / "no_budget" / "exceeds_budget"


def audit_concurrency(contracts: list[ResourceContract],
                      factors: list[ConcurrencyFactor],
                      *, raise_on_exceed: bool = False) -> list[AuditReport]:
    """乘法审计：按 scope 把各路径并发乘积加总，跟合同 max_concurrency 比。

    - 未声明合同的 scope → ok=False, reason='no_budget'（有需求没预算也是 bug）
    - 乘积 > 预算 → ok=False, reason='exceeds_budget'
    - raise_on_exceed=True 且任一 ok=False → 抛 concurrency_audit_failed（CI 卡点）
    返回每 scope 一份 AuditReport（报告式，给人读 / gate 打印）。
    """
    budget_by_scope = {c.scope: (c.capacity if c.capacity is not None else c.max_concurrency)
                       for c in contracts}
    by_scope: dict[str, list[ConcurrencyFactor]] = {}
    for f in factors:
        by_scope.setdefault(f.scope, []).append(f)

    reports: list[AuditReport] = []
    for scope in sorted(by_scope):
        fs = by_scope[scope]
        demanded = sum(f.demand() for f in fs)
        budget = budget_by_scope.get(scope)
        if budget is None:
            reports.append(AuditReport(scope, None, demanded, tuple(fs), False, "no_budget"))
        elif demanded > budget:
            reports.append(AuditReport(scope, budget, demanded, tuple(fs), False, "exceeds_budget"))
        else:
            reports.append(AuditReport(scope, budget, demanded, tuple(fs), True, ""))

    if raise_on_exceed:
        bad = [r for r in reports if not r.ok]
        if bad:
            raise ZeError(
                "concurrency_audit_failed",
                "乘法审计未通过：" + "；".join(
                    f"{r.scope} demanded={r.demanded} budget={r.budget}（{r.reason}）"
                    for r in bad),
                context={"failures": [
                    {"scope": r.scope, "demanded": r.demanded,
                     "budget": r.budget, "reason": r.reason} for r in bad]})
    return reports


__all__ = [
    "ResourceContract", "ResourceGate", "CapacityGate", "MachineGate", "MachineCapacityGate",
    "GateRegistry", "Probe",
    "ConcurrencyFactor", "AuditReport", "audit_concurrency",
    "validate_scope", "scope_is_recommended_domain",
    "detect_cpu_count", "detect_has_gpu", "detect_gpu_vram_mb", "detect_total_ram_mb",
    "ENFORCEMENT_PROCESS", "ENFORCEMENT_MACHINE",
    "ON_WAIT", "ON_REJECT", "ON_CANCEL_RETRY",
]
