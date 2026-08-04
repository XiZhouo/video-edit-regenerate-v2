# Resource Budget — 重资源架构合同 + 闸门 + 乘法审计

## 1. 这是什么

重资源（GPU 模型 / 外部 AI API / CPU 池 / 磁盘 IO）的**治理**叶子。它把一条工程纪律制度化：

> AI 可以写代码，但架构必须先由人写成约束。否则 AI 看到"并发炸了"就到处加 semaphore，每层看起来都限流，**入口一乘还是爆**。

所以每个重资源**先写一份架构合同**（资源 / 作用域 / 硬上限 / 唯一入口 / 违反时行为 / 验收），再写代码。本叶子把合同变成可执行机制：

1. **限流放资源入口，不放业务入口**——每个资源一个唯一闸门（`ResourceGate`/`MachineGate`），所有路径必经它。
2. **先定义全局预算再写代码**——预算是 `ResourceContract`，且按环境自动判断默认值（探测 CPU/GPU）。
3. **配置项名字带作用域**——`scope` 必须形如 `gpu.esrgan`，裸名 `max_workers` 被拒。
4. **并发做乘法审计**——`audit_concurrency` 把 路径数×每路径×子并发 的乘积跟预算比，发版前卡住。
5. **测并发边界 + 运行时自证**——闸门内部断言"活跃计数 ≤ 预算"，不靠相信代码；测试真起线程压上限。

### 为什么是独立叶子

| 它不是 | 它是 |
|---|---|
| 不是 [05-concurrency/](../05-concurrency/) 那种"提供工具、caller 自己决定放哪、未配置默认放行" | 是"先定义总量天花板 + 强制唯一入口 + 运行时自证 + 发版前审计" |
| 不是 [05-capacity-limit/](../../01-data-structure/05-capacity-limit/) 的数据规模上限（entity 数/字节） | 是运行时**重资源并发**的总量治理 |

05-concurrency 提供零件（Lock/Queue/RateLimiter），本叶子提供**治理**（合同 + 闸门 + 审计），并**组合**（不替代）它的 RateLimiter 做速率门。

### 覆盖范围：任何有限的设备/计算资源，不止 GPU

本叶子治理**所有有限的设备/计算资源**——GPU 计算与显存、系统内存、CPU 核、磁盘空间与 IO 带宽、网络带宽、文件句柄、DB 连接、外部 API 配额……ESRGAN 只是"槽位类"里举的一个例子，不是模板。

按第一性原理，设备资源的约束归三种 **kind**：

| kind | 约束本质 | 度量 | 现有原语 | 例子 |
|---|---|---|---|---|
| **槽位 slot** | 同时最多 N 个不可分实例 | 个数 | `max_concurrency`（Semaphore / machine slot） | GPU 同时几个重模型、CPU worker、子进程、DB 连接、文件句柄、API 并发数 |
| **吞吐 throughput** | 每单位时间最多 R 量 | 次/秒、MB/s | `rate_per_sec` + `acquire(rate_amount=量)`（速率门按量计） | API 次/秒、磁盘/网络带宽（rate_amount=字节数） |
| **容量 capacity** | 在用量之和 ≤ 总池（异构权重） | MB / GB | `capacity` + `CapacityGate.acquire(cost=用量)`，准入 `sum(cost) ≤ capacity` | GPU 显存、系统内存、磁盘空间 |

设备资源 → 怎么用现有原语建模：

| 设备资源 | 主 kind | 建模 | 备注 |
|---|---|---|---|
| GPU 计算（同时几个重模型） | 槽位 | `for_gpu_model`（max=1, machine） | ESRGAN 即此 |
| GPU 显存 VRAM | 容量 | `for_gpu_vram(per_job_mb=)`，`acquire(cost=峰值显存)` | `detect_gpu_vram_mb` 作总量；machine 级 ledger 跨进程 |
| 系统内存 RAM | 容量 | `for_memory_pool(per_job_mb=)`，`acquire(cost=峰值 RSS)` | `detect_total_ram_mb` 作总量（留 reserve） |
| CPU 核 | 槽位 | `for_cpu_pool`（cpu_count − reserve） | |
| 磁盘空间 | 容量 | `for_capacity_pool(capacity=可用字节)`，`acquire(cost=本次写入)` | |
| 磁盘 / 网络 IO 带宽 | 吞吐 | `rate_per_sec`=MB/s，`acquire(rate_amount=字节数)` | 速率门按量扣 token |
| 文件句柄 / DB 连接 | 槽位 | `max_concurrency` + 通用 `ResourceContract(...)` | |
| 外部 API | 槽位 + 吞吐 | `for_external_api`（并发 + 速率双限） | |

六个 `for_*` 工厂（槽位/吞吐：`for_gpu_model` / `for_cpu_pool` / `for_external_api`；容量：`for_gpu_vram` / `for_memory_pool` / `for_capacity_pool`）只是**便捷构造**；其余设备资源直接用通用构造 `ResourceContract(...)`（给 `capacity` 即容量类）或 `from_env`，机制完全一样（闸门/审计/自证不分资源种类）。

**容量类是一等公民（加权预算，非近似）**：GPU 显存 / 内存 / 磁盘空间的真实约束是"在跑任务的占用之和 ≤ 总量"，每个任务权重异构（A 吃 3G、B 吃 5G）。`CapacityGate.acquire(cost=本任务用量)` 直接表达这个语义：准入条件 `sum(已占) + cost ≤ capacity`，所以 8G 池里 3G+5G 能并存、5G+5G 不行。单任务 `cost > capacity` 立即抛 `resource_budget_invalid`（永远装不下，等也没用）。跨进程共享的容量（整卡显存 / 系统内存）用 `MachineCapacityGate`（fcntl-locked ledger）保证同机所有进程 `sum(cost) ≤ capacity`。`max_concurrency` 字段对容量合同仅记"最坏并发数"（`floor(capacity/per_job)`）供展示，不参与加权门控。

## 2. 核心组件

| 组件 | 作用 |
|---|---|
| `ResourceContract` | 架构合同的代码化身（frozen dataclass）。`max_concurrency`=槽位类；`capacity`=容量类（`.kind` 区分） |
| `ResourceGate` | 槽位类进程内闸门：速率门 + 并发门（BoundedSemaphore）+ 运行时自证 |
| `CapacityGate` | 容量类进程内闸门：速率门 + 加权容量门（Condition 计数 `sum(cost)≤capacity`）+ 运行时自证 |
| `MachineGate` | 槽位类单机跨进程闸门：fcntl slot 文件锁（如 ESRGAN 多进程） |
| `MachineCapacityGate` | 容量类单机跨进程闸门：fcntl-locked ledger（加权 `sum(cost)≤capacity`，如整卡显存/内存） |
| `GateRegistry` | 同 scope 进程内唯一登记处，按 (scope, kind, enforcement_scope) 路由四种闸门 |
| `audit_concurrency` | 乘法审计：各路径需求乘积（含 cost）vs 预算（槽位比 max_concurrency / 容量比 capacity） |
| `Probe` | 验收探针：实测真实占用 vs 硬上限（含 `pgrep` / `held_units` 计数） |
| `validate_scope` | 作用域命名强校验 |
| `detect_*` | 环境探测（cpu_count / has_gpu / gpu_vram / total_ram），零重依赖 |

它们独立工作；四种闸门都组合 05-concurrency 的 `RateLimiter` 做速率门。

## 3. ResourceContract + 环境自适应

合同字段对齐"架构合同"要素：

| 合同要素 | 字段 | 说明 |
|---|---|---|
| 资源 | `resource: str` | 人类可读名，如 "ESRGAN" |
| 作用域 | `enforcement_scope: str` | `process`（单进程多线程）/ `machine`（单机全局多进程）——决定闸门后端 |
| （命名作用域） | `scope: str` | 带作用域 key，如 `gpu.esrgan`（原则 3） |
| 硬上限（槽位） | `max_concurrency: int` | 并发个数（槽位类主预算；容量类记最坏并发数，展示用） |
| 硬上限（容量） | `capacity: int\|None` | 容量单位池（如显存/内存 MB）；给值即"容量类"，闸门按 `sum(cost)≤capacity` 加权准入 |
| 硬上限（吞吐） | `rate_per_sec: float\|None` | 可选速率（API / 带宽） |
| 违反时行为 | `on_violation: str` | `wait`（等待，不丢任务，不额外启动）/ `reject` / `cancel_retry`（取消未完成、retry 重申 token） |
| 唯一入口 | （机制）`ResourceGate / CapacityGate.acquire()` | 见 §5 |
| 验收 | （机制）`Probe` + 并发边界测试 | 见 §5 / 测试 |

`.kind` 属性：`capacity` 给值 → "容量"（CapacityGate）；否则 → "槽位"（ResourceGate）。
`__post_init__` 校验：scope 合法、max_concurrency≥1（且不是 bool）、enforcement_scope/on_violation 在枚举内、rate 若给必 >0、capacity 若给必是 ≥1 整数。

### 3.1 环境自适应工厂（探测放工厂，dataclass 保持纯值对象）

| 工厂 | 默认推导 | 理由 |
|---|---|---|
| `for_gpu_model(scope)` | `max_concurrency=1`，`enforcement_scope=machine`，`on_violation=wait` | 单 GPU 同时跑多个重模型必爆显存；重模型常是独立子进程，单机全局才挡得住 |
| `for_cpu_pool(scope, reserve=1)` | `max(1, cpu_count - reserve)` | 留核给主线程/IO |
| `for_external_api(scope, rate_per_sec, max_concurrency)` | 来自 API 文档（不探测），`on_violation=cancel_retry` | 速率天花板是 API 合同，不是本机能力 |
| `for_gpu_vram(scope, per_job_mb, total_mb=None)` | `capacity=detect_gpu_vram_mb()/env`（缺省 8192），`enforcement_scope=machine` | 整卡显存被同机所有进程共享，machine ledger 才挡得住 |
| `for_memory_pool(scope, per_job_mb, reserve_mb=512)` | `capacity=detect_total_ram_mb()-reserve`（探测失败 4096） | 系统内存容量；留 reserve 给系统 |
| `for_capacity_pool(scope, capacity, per_job=None)` | 通用容量池；env `ZE_RB_<SCOPE>_CAPACITY` 只能下调 | 磁盘空间等任意容量资源 |

前三个是槽位/吞吐类，后三个是容量类。其余设备资源直接用通用构造 `ResourceContract(...)`（给 `capacity` 即容量类）或 `from_env`——闸门、乘法审计、运行时自证都不分资源种类。吞吐类（带宽）给 `rate_per_sec` 并 `acquire(rate_amount=字节数)`。

### 3.2 探测信号（零重依赖、绝不抛错、保守降级，**绝不 import torch/pynvml**）

| 函数 | 信号 | miss 时 |
|---|---|---|
| `detect_cpu_count()` | `os.cpu_count()` | 返 1 |
| `detect_has_gpu(env)` | `ZE_RB_FORCE_GPU` → `CUDA_VISIBLE_DEVICES`（非空非'-1'）→ `/dev/nvidia*` → `which nvidia-smi` | 保守当无 GPU（False） |
| `detect_gpu_vram_mb(env)` | 仅 `ZE_RB_GPU_VRAM_MB` 显式给值 | None（不主动读 nvidia-ml） |
| `detect_total_ram_mb()` | `os.sysconf(SC_PHYS_PAGES × SC_PAGE_SIZE)` → `/proc/meminfo` | None（不引 psutil） |

`detect_gpu_vram_mb` 被 `for_gpu_vram()` 消费作显存容量池总量（缺省保守 8192）；`detect_total_ram_mb` 被 `for_memory_pool()` 消费作内存容量池总量（探测失败保守 4096）。磁盘空间等其它容量本叶子不内置探测；mod 用 `shutil.disk_usage` 等算出后传 `for_capacity_pool(capacity=)`。

### 3.3 覆盖优先级（高→低）

1. caller 显式构造 `ResourceContract(...)`
2. env `ZE_RB_<SCOPE>_MAX_CONCURRENCY` / `ZE_RB_<SCOPE>_RATE_PER_SEC`（scope 的 `.` 转 `_` 大写，如 `gpu.esrgan` → `ZE_RB_GPU_ESRGAN_*`）
3. 探测推导值
4. 工厂保守 fallback

`for_external_api` 的 env 只能**下调**（`min(env, 合同值)`），不可上调超过 API 文档上限。env 非法/非正抛 `resource_budget_invalid`（对齐 capacity 的 `capacity_config_invalid`）。

## 4. 作用域命名规则（原则 3）

```
<domain>.<resource>[.<sub>[.<sub>]]   —— 2~4 段，全小写，段内字母开头 + 字母数字
正则: ^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*){1,3}$
```

合法：`gpu.esrgan` / `api.openai.chat` / `cpu.pool` / `io.disk.write`
非法：裸名 `max_workers`、大写 `GPU.x`、单段 `gpu`、空串 → `resource_scope_invalid`

推荐 domain（第一段，仅 `scope_is_recommended_domain` 自检，不强制 fail）：`gpu` / `cpu` / `api` / `io` / `mem`。

## 5. 四种闸门 + 运行时自证

槽位类（`ResourceGate` 进程内 / `MachineGate` 单机）+ 容量类（`CapacityGate` 进程内 / `MachineCapacityGate` 单机）。`GateRegistry.get_or_create` 按 (kind, enforcement_scope) 自动选。

### 5.1 两道串联门

`acquire()` 是上下文管理器，内部**两道门串联**：① 速率门（`RateLimiter.wait_for`，管"每秒几次/几量"）② 主门（槽位类=并发门 `BoundedSemaphore`/slot 文件锁，管"同时几个"；容量类=加权容量门 `Condition` 计数/ledger，管"在用量之和"）。都过才进 `with` 体；退出（含异常）自动释放（速率 token 不退还，符合 token bucket 语义；并发槽 / 容量单位归还）。

槽位类 `acquire(*, rate_amount=1.0, timeout=)`：`rate_amount` 只作用于速率门（吞吐按量，如带宽 `rate_amount=字节数`）；并发门恒占 1 个槽。
容量类 `acquire(*, cost, rate_amount=1.0, timeout=)`：`cost` = 本任务用量，从 capacity 单位池加权扣减，准入 `sum(cost) ≤ capacity`；单任务 `cost > capacity` 立即抛 `resource_budget_invalid`（永远装不下）。

`on_violation` 决定拿不到时的行为：`wait` 无限等（或按显式 `timeout`）；`reject`/`cancel_retry` 不等，立即抛 `resource_unavailable`（`cancel_retry` 由 caller 负责 retry 重新 `acquire`）。

```python
gate = ResourceGate(contract)
with gate.acquire(timeout=30):
    call_heavy_resource()        # 真正调资源的代码放 with 体内
```

### 5.2 进程内 vs 单机全局

| | `ResourceGate`（process） | `MachineGate`（machine） |
|---|---|---|
| 后端 | `threading.BoundedSemaphore` | fcntl slot 文件锁（`lock_dir` 下 N 个 slot 文件） |
| 管得住 | 单进程多线程 | 单台机器**所有进程** |
| 适用 | 进程内线程池、API 客户端 | ESRGAN/upscayl-bin 等独立子进程 |
| 自证 | 活跃计数断言（见 5.3） | OS 文件锁物理保证 slot 数 ≤ N |

**为什么 ESRGAN 必须 machine**：若生成器跑多个 Python 进程，每个进程的进程内 semaphore 各看各的——合起来照样 >N，正是"入口一乘还是爆"。`MachineGate` 用 fcntl flock 锁 OFD（open file description），跨进程互斥，物理上只有 N 个 slot。`lock_dir` 默认 `$ZE_RB_LOCK_DIR/<scope>`（缺省 `/tmp/ze_rb/...`），同机所有进程共享同一目录才互斥。

### 5.3 运行时自证（原则 5）

`ResourceGate` 进体前在临界区断言 `_active ≤ max_concurrency`，违反抛 `resource_budget_exceeded`。因为 semaphore 已挡住，这个断言**理论上永不触发**——它的价值正是"不靠相信代码写对了"：如果有人误把同 scope 建了两个 semaphore、或改了预算没改 semaphore 容量，断言会当场抓住（测试 `test_self_attestation_fires_on_misconfigured_gate` 故意制造畸形闸门验证它非摆设）。

`Probe` 是外部验收探针：`Probe.from_gate(gate)` 读闸门 active（槽位类=活跃个数 / 容量类=在用单位）；`Probe.pgrep_count("upscayl-bin")` 数真实进程数。对应合同"验收"字段（如 ESRGAN：压力测试中 `pgrep upscayl-bin ≤ 3`）。

### 5.4 容量类闸门（CapacityGate / MachineCapacityGate）

| | `CapacityGate`（process） | `MachineCapacityGate`（machine） |
|---|---|---|
| 后端 | `threading.Condition` + 单位计数器（`_available`/`_in_use`） | fcntl-locked `ledger.json`（`[{token,pid,cost,ts}]`） |
| 准入 | `wait_for(_available ≥ cost)` 后扣 cost | 锁 ledger → 清死进程残留 → `sum(cost)+cost ≤ capacity` 则追加条目 |
| 管得住 | 单进程多线程的加权总量 | 单台机器**所有进程**的加权总量（整卡显存/系统内存） |
| 自证 | 临界区断言 `_in_use ≤ capacity`（畸形闸门触发 `resource_budget_exceeded`） | ledger 在文件锁下读改写，`sum ≤ capacity` 物理保证 |
| 死进程 | 不涉及（线程内 release 归还） | `os.kill(pid,0)` 存活检查，每次 acquire/held_units 清残留 |

**为什么显存/内存必须 machine 容量**：整卡显存被同机所有进程共享，进程内 `CapacityGate` 各算各的池——合起来照样超总显存。`MachineCapacityGate` 用一个 fcntl 排他锁保护的 ledger，所有进程读改写同一份"在用量之和"，物理保证 `sum(cost) ≤ capacity`。`lock_dir` 默认 `$ZE_RB_LOCK_DIR/<scope>`，同机所有进程共享同一目录才互斥。

`MachineCapacityGate.held_units()` 返回当前在用单位之和（跨进程探针，验收用）。

## 6. 边界 + "无法绕过"的诚实限度

| 跟谁 | 边界 |
|---|---|
| [05-concurrency/](../05-concurrency/) | 本叶子**组合** RateLimiter（速率门），不重写 token bucket；不做调度（调度仍是 QueueManager/上层工程层，沿用 AP-CC2） |
| [05-capacity-limit/](../../01-data-structure/05-capacity-limit/) | 正交：那管数据规模，这管运行时资源并发；互不引用 |

**"无法绕过"是软约束**：Python 没有真正的访问控制。闸门提供"最方便的正路"——唯一公开入口是 `acquire()` 上下文管理器，`GateRegistry` 保证同 scope 唯一。但若有人完全绕过闸门直接调资源，闸门拦不住。兜底是**乘法审计**（发版前抓声明层的总量）+ **运行时自证**（抓真经过闸门的活跃计数）。这点必须诚实说明，不假装能拦住野路径。

## 7. 集成 pattern

```python
# framework 启动时建单例 registry + 注册各资源合同（槽位 + 容量混用）
registry = GateRegistry()
registry.get_or_create(ResourceContract.for_gpu_model("gpu.esrgan", resource="ESRGAN"))     # 槽位
registry.get_or_create(ResourceContract.for_external_api("api.gen", rate_per_sec=5, max_concurrency=3))
registry.get_or_create(ResourceContract.for_gpu_vram("mem.vram", per_job_mb=3000))          # 容量

# command handler / pipeline stage 里：唯一入口
def run_upscale(args):
    with registry.get("gpu.esrgan").acquire():           # 槽位, machine, 恒占 1
        subprocess.run(["upscayl-bin", ...])

def run_inference(args, peak_mb):
    with registry.get("mem.vram").acquire(cost=peak_mb): # 容量, 加权, sum(cost)<=总显存
        model.infer(...)

# 发版前 / CI: 乘法审计（容量 scope 的 cost = 单任务用量）
contracts = [registry.get(s).contract for s in registry.scopes()]
factors = [ConcurrencyFactor("gpu.esrgan", "pipeline:upscale", path_count=4, per_path=2),    # 8 > 1!
           ConcurrencyFactor("mem.vram", "pipeline:infer", path_count=4, cost=3000)]         # 12000 > 8192!
audit_concurrency(contracts, factors, raise_on_exceed=True)   # 抛 concurrency_audit_failed
```

## 8. 红线 + 反模式

红线：
- **限流在资源入口，不在业务入口** —— 一个 scope 一个闸门，所有路径经它（原则 1）
- **先有合同再写代码** —— 被 acquire 的 scope 必先有 ResourceContract（原则 2）
- **配置项带作用域** —— scope 必须点分命名（原则 3）
- **探测零重依赖** —— 绝不 import torch/pynvml

反模式：
- **AP-RB-1** 每条业务路径各自加 semaphore（"看起来都限流，入口一乘还是爆"）→ 应统一到资源入口闸门
- **AP-RB-2** 配置裸名 `max_workers` / `concurrency`（谁的？）→ 必须 `gpu.esrgan` 式带作用域
- **AP-RB-3** 不声明合同就 acquire / 直接 new 第二个同 scope 闸门 → 走 GateRegistry 唯一入口
- **AP-RB-4** ESRGAN 用 `ResourceGate`（进程内）但实际多进程 → 必须 `MachineGate`（enforcement_scope=machine）
- **AP-RB-5** 乘法审计漏算 sub 并发（只算 path×per_path）→ 三层都要乘
- **AP-RB-6** 探测引重依赖（import torch 判 GPU）→ 只用 env + 文件系统 + which 嗅探
- **AP-RB-7** catch 掉 `resource_budget_exceeded` 默默继续 → 自证失败说明闸门错配，应让它冒出修架构
- **AP-RB-8** 容量类资源（显存/内存）当槽位类管（`max_concurrency` 切等大槽）→ 异构权重下要么爆要么浪费；应给 `capacity` 用 `CapacityGate.acquire(cost=本任务用量)`
- **AP-RB-9** 容量类 `acquire()` 漏传 `cost`（或恒传 1）→ 退化成"按个数"，加权失效；cost 必须是本任务真实用量
- **AP-RB-10** 跨进程共享的容量（整卡显存/系统内存）用进程内 `CapacityGate` → 各进程各算各池，合起来超总量；必须 `MachineCapacityGate`（enforcement_scope=machine）

## 9. Python API

```python
from resource_budget import (
    ResourceContract, ResourceGate, CapacityGate, MachineGate, MachineCapacityGate,
    GateRegistry, Probe, ConcurrencyFactor, AuditReport, audit_concurrency,
    validate_scope, detect_cpu_count, detect_has_gpu, detect_gpu_vram_mb, detect_total_ram_mb,
    ENFORCEMENT_PROCESS, ENFORCEMENT_MACHINE, ON_WAIT, ON_REJECT, ON_CANCEL_RETRY,
)

# 合同（capacity 给值 → 容量类；否则槽位类）
ResourceContract(resource, scope, max_concurrency, *, enforcement_scope="process",
                 rate_per_sec=None, rate_burst=None, on_violation="wait", capacity=None)
ResourceContract.for_gpu_model(scope, *, resource="", env=None, on_violation="wait",
                               enforcement_scope="machine")     # 槽位; 默认 max=1, machine
ResourceContract.for_cpu_pool(scope, *, resource="", env=None, reserve=1, on_violation="wait")
ResourceContract.for_external_api(scope, *, rate_per_sec, max_concurrency, resource="",
                                  env=None, rate_burst=None, on_violation="cancel_retry",
                                  enforcement_scope="process")
ResourceContract.for_gpu_vram(scope, *, per_job_mb, total_mb=None, resource="GPU-VRAM",
                              env=None, on_violation="wait")    # 容量; machine
ResourceContract.for_memory_pool(scope, *, per_job_mb=None, total_mb=None, reserve_mb=512,
                                 resource="system-memory", env=None, on_violation="wait",
                                 enforcement_scope="machine")   # 容量
ResourceContract.for_capacity_pool(scope, *, capacity, resource="", per_job=None, env=None,
                                   enforcement_scope="machine", on_violation="wait",
                                   rate_per_sec=None)           # 容量（通用）
ResourceContract.from_env(*, defaults, env=None)               # env 逐字段覆盖（含 capacity 下调）
contract.kind  # → "slot" | "capacity"

# 槽位类闸门
gate = ResourceGate(contract, *, rate_limiter=None)
with gate.acquire(*, timeout=None, rate_amount=1.0): ...        # 恒占 1 槽
gate.stats()  # → {scope, max_concurrency, active, peak, available, enforcement_scope}
mgate = MachineGate(contract, *, lock_dir=None, rate_limiter=None, poll_interval=0.05)
with mgate.acquire(*, timeout=None, rate_amount=1.0): ...
mgate.held_count()  # → int

# 容量类闸门（加权 cost）
cgate = CapacityGate(contract, *, rate_limiter=None)
with cgate.acquire(*, cost, timeout=None, rate_amount=1.0): ... # 扣 cost 单位
cgate.stats()  # → {scope, capacity, max_concurrency, active(in_use), peak, available, ...}
mcgate = MachineCapacityGate(contract, *, lock_dir=None, rate_limiter=None, poll_interval=0.05)
with mcgate.acquire(*, cost, timeout=None, rate_amount=1.0): ...
mcgate.held_units()  # → int (跨进程在用单位之和)

# 登记处（按 kind/enforcement_scope 路由四种闸门）
reg = GateRegistry()
reg.register(gate); reg.get(scope); reg.get_or_create(contract, **kw); reg.scopes()

# 探针
Probe(scope, budget, *, count_fn).current() / .assert_within_budget()
Probe.from_gate(gate);  Probe.pgrep_count("upscayl-bin")

# 乘法审计（cost：槽位类=1；容量类=单任务用量。容量 scope 的预算按 capacity 比）
ConcurrencyFactor(scope, path_label, path_count=1, per_path=1, sub=1, cost=1).demand()
audit_concurrency(contracts, factors, *, raise_on_exceed=False) -> list[AuditReport]
```

错误码：`resource_budget_invalid`（配置非法）/ `resource_scope_invalid`（命名非法）/ `resource_scope_duplicate`（同 scope 重复注册）/ `resource_gate_not_found`（未注册）/ `resource_unavailable`（拿不到资源，按 on_violation）/ `resource_budget_exceeded`（运行时自证失败）/ `concurrency_audit_failed`（乘法审计超预算）。

## 10. mod 端注意

业务方在 [02-mod/06-resources.md](../../../02-mod/06-resources.md) 声明本 skill 的重资源合同（6 字段）+ 各调用路径的并发声明（乘法审计输入）+ 闸门单例放哪 / handler 里怎么 acquire。framework 启动时建 `GateRegistry` 单例并注册各合同；命令 handler / pipeline stage 调外部资源前 `with registry.get(scope).acquire():`。

## 11. 当前实装状态

### ✅ 已实装（M1，无跨叶子依赖）

| 项 | 说明 |
|---|---|
| `ResourceContract`（slot/capacity 双 kind）frozen + 6 工厂 + `from_env` | 字段对齐架构合同；`.kind` 区分；环境自适应探测 |
| `detect_cpu_count/has_gpu/gpu_vram_mb/total_ram_mb` | 零重依赖、绝不抛错、保守降级 |
| `validate_scope` + 命名正则 | 原则 3 |
| 槽位类：`ResourceGate`（process Semaphore）/ `MachineGate`（fcntl slot） | 并发门 + 组合 RateLimiter 速率门 + 运行时自证 |
| **容量类：`CapacityGate`（process Condition 加权）/ `MachineCapacityGate`（跨进程 fcntl ledger）** | 加权容量门 `sum(cost)≤capacity` + 速率门 + 运行时自证；`detect_gpu_vram_mb/total_ram_mb` 被消费 |
| `GateRegistry` | 同 scope 唯一；按 (kind, enforcement_scope) 路由四种闸门 |
| `Probe` | 验收探针 + `pgrep_count`（槽位）/ `held_units`（machine 容量） |
| `audit_concurrency` + `ConcurrencyFactor`（含 cost） | 乘法审计；槽位比 max_concurrency / 容量比 capacity |
| audit gates RB-01..06 | runtime（含 RB-06 容量加权预算；见 [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md)） |
| 并发边界测试 64 个 | 槽位峰值/串行/死锁超时/自证 + 容量加权准入/异构/超cost拒/ledger 跨进程/审计 |
| RateLimiter 线程安全 | 配套修了 [05-concurrency/rate.py](../05-concurrency/rate.py) |

### ⏳ 待补（M2，真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| 乘法审计自动读 pipeline `fan_out` | [03-pipeline/](../../03-commands-operations/03-pipeline/) 暴露并发声明 API；当前靠 mod 手写 |
| 闸门拿不到槽转交 QueueManager 排队报 ETA | [05-concurrency/](../05-concurrency/) QueueManager 组合 |
| 静态 gate RB-07/08（声明先于使用 / 资源入口非业务入口） | 等 AST/lint 工具（跟现有 CC-01/02/03 同批） |
| 多机（跨机器）分布式锁 | 走 05-concurrency Redis 锁待补；`MachineCapacityGate` 只管单机 |

### 原则

- **API 签名先定**：`ResourceContract` / `acquire` / `audit_concurrency` 签名固定。
- **合同先于代码**：每个重资源先写合同，再让 AI/人据此写代码。
- **默认值可调**：探测推导仅作起点；真生产数据决定如何调。
