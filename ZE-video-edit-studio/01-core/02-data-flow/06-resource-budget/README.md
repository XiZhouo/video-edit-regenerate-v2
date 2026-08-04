# 06-resource-budget — 重资源架构合同 + 闸门 + 乘法审计

> 一句话：每个**有限的设备/计算资源**（GPU 计算与显存 / 系统内存 / CPU / 磁盘与网络 IO / 文件句柄 / 外部 API …）**先写一份架构合同**（资源/作用域/硬上限/唯一入口/违反时行为/验收），再写代码。合同变成运行时强制的唯一入口闸门 + 发版前乘法审计 + 运行时自证。治理 [05-concurrency/](../05-concurrency/) 工具背后的"总量"。
>
> 不止 GPU/ESRGAN——ESRGAN 只是"槽位类"的一个例子。设备资源约束分三种 kind：**槽位**（个数，`max_concurrency`，ResourceGate/MachineGate）/ **吞吐**（每秒量，`rate_per_sec` + `acquire(rate_amount=量)`）/ **容量**（在用量之和 ≤ 池，如显存/内存，`capacity` + `CapacityGate.acquire(cost=用量)` 加权一等公民）。完整映射表见 [SPEC.md §1](./SPEC.md#1-这是什么)。

## 在框架里的位置

- 上一级：[02-data-flow/](../) — 数据流叶子
- 邻居：[05-concurrency/](../05-concurrency/) 提供零件（Lock/Queue/RateLimiter）；本叶子提供**治理**，并组合它的 RateLimiter 做速率门
- 跟 [05-capacity-limit/](../../01-data-structure/05-capacity-limit/) 正交：那管数据规模（entity 数/字节），这管运行时重资源并发

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览） |
| [SPEC.md](./SPEC.md) | 11 节完整规则（合同 / 探测 / 命名 / 闸门 / 自证 / 审计 / 反模式） |
| [resource_budget.py](./resource_budget.py) | ResourceContract + 四种闸门（ResourceGate/CapacityGate/MachineGate/MachineCapacityGate）+ GateRegistry + audit_concurrency + Probe + 探测 + validate_scope |
| [test_resource_budget.py](./test_resource_budget.py) | pytest（64 个，槽位并发边界 + 容量加权准入 + 自证 + machine slot/ledger） |

## 当前状态

**✅ 已实装（M1，无跨叶子依赖）**
- `ResourceContract`（槽位/容量双 kind）+ 6 个环境自适应工厂 + from_env
  - 槽位/吞吐：for_gpu_model / for_cpu_pool / for_external_api
  - 容量：for_gpu_vram / for_memory_pool / for_capacity_pool
- 探测 `detect_cpu_count / detect_has_gpu / detect_gpu_vram_mb / detect_total_ram_mb`（零重依赖、绝不抛错、保守降级）
- `validate_scope`（作用域命名强校验）
- 槽位类闸门：`ResourceGate`（进程内 BoundedSemaphore）/ `MachineGate`（fcntl slot 文件锁）
- 容量类闸门：`CapacityGate`（进程内 Condition 加权计数）/ `MachineCapacityGate`（跨进程 fcntl ledger）
- 四种闸门都：组合 RateLimiter 速率门 + 运行时自证；`GateRegistry` 按 (kind, enforcement_scope) 唯一路由
- `Probe`（验收探针，含 pgrep / held_units 计数）+ `audit_concurrency`（乘法审计，含 cost）
- 配套修了 [05-concurrency/rate.py](../05-concurrency/rate.py) 的 RateLimiter 线程安全

**⏳ 待补（M2，真有跨叶子依赖）**
- 乘法审计自动从 [03-pipeline/](../../03-commands-operations/03-pipeline/) 读 `fan_out`（当前靠 mod 手写声明）
- 闸门拿不到槽转交 [05-concurrency/](../05-concurrency/) QueueManager 排队报 ETA
- 静态 gate RB-07/08（等 AST/lint 工具）
- 多机（跨机器）分布式锁（走 05-concurrency Redis 锁待补；MachineCapacityGate 只管单机）

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态)。

## 怎么用

### 写一份合同（环境自适应）

```python
from resource_budget import ResourceContract, GateRegistry

# 槽位类 —— GPU 重模型: 默认 max=1, machine 级 (多进程也挡得住), 等待不丢任务
esrgan = ResourceContract.for_gpu_model("gpu.esrgan", resource="ESRGAN")
# 槽位+吞吐类 —— 外部 API: 速率/并发来自 API 文档, 取消未完成请求由 caller retry
api = ResourceContract.for_external_api("api.gen", rate_per_sec=5.0, max_concurrency=3)

# 容量类 —— GPU 显存: 加权预算, total 自动探测 detect_gpu_vram_mb (缺省 8192), machine 级 ledger
vram = ResourceContract.for_gpu_vram("mem.vram", per_job_mb=3000)
# 容量类 —— 系统内存: total 自动探测 detect_total_ram_mb (留 reserve), acquire(cost=峰值 RSS)
mem = ResourceContract.for_memory_pool("mem.rss", per_job_mb=2000, reserve_mb=1024)

# 吞吐类 —— 磁盘写带宽: rate=MB/s, 调用方 acquire(rate_amount=本次 MB 数)
disk = ResourceContract(resource="disk-write", scope="io.disk.write",
                        max_concurrency=64, rate_per_sec=200.0)  # 200 MB/s
```

> 容量类（显存/内存/磁盘空间）是**加权一等公民**：`CapacityGate.acquire(cost=本任务用量)`，准入 `sum(cost)≤capacity`（异构权重，A 吃 3G、B 吃 5G，8G 池里 A+B 行、5G+5G 不行）。跨进程共享的容量（整卡显存/内存）用 `MachineCapacityGate`（fcntl ledger）。

### 唯一入口闸门

```python
registry = GateRegistry()                 # framework 启动时建单例
registry.get_or_create(esrgan)            # → MachineGate (槽位)
registry.get_or_create(api)               # → ResourceGate (槽位+速率)
registry.get_or_create(vram)              # → MachineCapacityGate (容量)

# 槽位类: 恒占 1
with registry.get("gpu.esrgan").acquire():
    subprocess.run(["upscayl-bin", ...])   # machine 级, 跨进程挡住

# 容量类: 加权 cost
with registry.get("mem.vram").acquire(cost=3000):
    model.infer(...)                       # 同机在跑任务的显存之和 <= 总显存
```

### 发版前乘法审计

```python
from resource_budget import ConcurrencyFactor, audit_concurrency

contracts = [registry.get(s).contract for s in registry.scopes()]
factors = [ConcurrencyFactor("gpu.esrgan", "pipeline:upscale", path_count=4, per_path=2)]  # 8
audit_concurrency(contracts, factors, raise_on_exceed=True)   # 8 > 1 → 抛 concurrency_audit_failed
```

### 验收（运行时自证）

```python
from resource_budget import Probe
# 压力测试中断言真实进程数不超预算 (对应合同"验收"字段)
assert Probe.pgrep_count("upscayl-bin") <= 3
```

## 设计要点

- **合同先于代码**：人先写架构合同，AI/人据此写代码，成功率高很多。
- **限流在资源入口**：一个 scope 一个闸门，不在业务路径各自加 semaphore（避免"入口一乘还是爆"）。
- **环境自适应**：探测 CPU/GPU 推默认预算，零重依赖（绝不 import torch/pynvml）。
- **作用域命名**：`gpu.esrgan` 式点分，裸名被拒。
- **容量加权一等公民**：显存/内存按 `sum(cost) ≤ capacity` 加权准入（异构权重），不退化成"切等大槽"；跨进程靠 fcntl ledger 物理保证。
- **运行时自证**：闸门断言在用量 ≤ 预算，不靠相信代码；machine 级靠 OS 文件锁物理保证。
- **"无法绕过"是软约束**：兜底靠乘法审计 + 自证；不假装能拦住完全绕过的野路径（SPEC §6 诚实声明）。

## audit gates

注册到 [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md)：RB-01（budget_from_env）/ RB-02（scope_naming_enforced）/ RB-03（gate_single_entry_per_scope）/ **RB-04（runtime_active_le_budget，槽位运行时自证）** / **RB-05（concurrency_audit_multiplication，乘法审计）** / **RB-06（capacity_weighted_le_budget，容量加权预算 + 超 cost 拒 + 自证）** 为 runtime；RB-07/08 为 static（提示性，待 AST 工具）。

## 跑测试

```bash
cd 01-core/02-data-flow/06-resource-budget/
python -m pytest test_resource_budget.py -v
```
