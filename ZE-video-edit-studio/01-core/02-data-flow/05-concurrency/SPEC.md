# Concurrency — 并发访问控制

## 1. 这是什么

服务端处理多个并发请求时怎么保证：

- 同一 entity 同一时刻只有一个写者（编辑层互斥）
- 长任务排队（避免 AI / 重计算把 server 打爆）
- 限流（AI API 每秒只能调 5 次，多了排队）

这三件事属于**访问层**——把并发请求**串行化 / 排队 / 节流**，让下游执行逻辑（command / pipeline）拿到的请求是有序的、不过载的。

如果串行化后**数据层面仍有逻辑冲突**（A 改了字段 X，B 也改了 X，base 不同），那是 [02-conflict-resolution/](../02-conflict-resolution/) 叶子的事。本叶子只管访问层。

本叶子提供**零件**（Lock / Queue / RateLimiter），不管"总量天花板"。任何有限设备资源（GPU 计算与显存 / 系统内存 / CPU / 磁盘与网络 IO / 外部 API …）的总量治理——先写架构合同、强制唯一入口闸门、发版前乘法审计、运行时自证——是邻叶 [06-resource-budget/](../06-resource-budget/) 的事，它**组合**（不替代）本叶子的 RateLimiter 做速率门。

## 2. 三个核心组件

| 组件 | 解决什么 |
|---|---|
| **AdvisoryLockManager** | 编辑互斥：UI 上点"编辑"按钮拿锁；别人看到"张三在编辑"；force 接管 |
| **QueueManager** | 长任务排队：用户提交 AI 生成请求 → 进队 → 报告 position + ETA → 调度跑 |
| **RateLimiter** | 限流：AI API 每秒最多 N 次；token bucket |

它们独立工作，调用方按需用。本叶子**不强制集成到 command / pipeline**——业务方自己决定哪些 command 拿锁、哪些 stage 进队、哪些 API 调用走限流。

## 3. AdvisoryLockManager — 编辑互斥锁

**用途**：UI 编辑层的"软锁"——用户点"编辑某 entity"时调 `acquire(...)`，别的用户来编辑同一 entity 时拒绝（或显示"张三在编辑，是否强制接管"）。

**为什么叫 advisory**：不是数据库行锁那种硬锁。本质是个状态查询：lock 表存"谁拿了哪个 entity 的编辑权"，UI 层据此决策。**绕过 lock 直接 dispatch command 仍能改数据**——`AdvisoryLock` 不阻止变更，只阻止"编辑 UI 抢同一 entity"。

数据保护层是 CAS（`expected_version`），那是真硬约束。

### 数据结构

```python
@dataclass(frozen=True)
class LockHandle:
    lock_id: str                                # uuid; release 时用作校验
    entity_type: str
    entity_id: str
    holder_user: str                            # 谁拿的锁
    holder_device: str = "default"
    acquired_at: float
    expires_at: float                           # TTL 过期自动释放
```

### API

```python
class AdvisoryLockManager:
    def __init__(ttl_sec: float = 300.0)        # 5 分钟 TTL 默认

    def acquire(*, entity_type, entity_id, user_id, device_id="default",
                force: bool = False) -> LockHandle
        # 已被别人锁: 抛 acl_denied 含 holder_user / expires_at; force=True 强制接管
        # 自己已锁: 续期 (返回新 handle)

    def release(handle: LockHandle) -> bool      # lock_id 校验; 不是自己的锁返 False

    def state(entity_type, entity_id) -> LockHandle | None
        # 查锁状态 (过期自动清); 用于 UI 展示"张三在编辑"

    def sweep_expired() -> int                   # 主动清过期; 返回清了几个
```

### TTL / 续期 / force 接管

- **TTL**：默认 5 分钟。用户编辑期间应**周期性续期**（重新 `acquire`），保持 lock 活
- **过期自动释放**：`state()` / `acquire()` 调用时顺手清过期；显式 `sweep_expired()` 也可
- **自己已锁不算冲突**：同 user + device 重新 acquire 等同续期
- **force 接管**：管理员场景 / 长时间无响应；接管时**应该记审计**（caller 负责 log 谁强制接管了谁的锁）

### 跟 CAS 的关系

- `AdvisoryLock` 阻止"两个用户的 UI 同时点编辑同一 entity"
- `CAS`（entity.update 的 expected_version）阻止"数据真被覆盖"
- 两者**互补不替代**：拿了 lock 也得在 update 时传 expected_version；不拿 lock 也能 update（但用户体验会冲突）

## 4. QueueManager — 任务排队

**用途**：长任务（pipeline / stage / AI 调用）要排队执行——比如 server 上同时只允许跑 2 个 AI generation，超出的排队。给前端报告 position + ETA。

**为什么**：长任务无脑并发会导致：
- 内存爆（每个 AI 调用占 1 GB）
- API 配额穿透（限流被绕过）
- 用户感受不到"在跑"（看不到 position）

排队解决以上三个。

### 数据结构

```python
@dataclass
class QueueItem:
    task_id: str                                 # uuid
    stage_id: str                                # 按 stage 分组
    priority: int = 0                            # 高优先级先跑（数值大=优先）
    enqueued_at: float
```

### API

```python
class QueueManager:
    def enqueue(stage_id: str, *, priority: int = 0) -> QueueItem
        # 入队; 同 priority 内按 enqueued_at FIFO

    def position(task_id: str) -> int | None     # 当前队列位置（1-indexed）; 不存在返 None

    def estimate_eta_sec(task_id: str) -> float  # ETA: 基于历史 P90 延迟 × 前面任务数

    def start(task_id: str) -> bool              # 任务开始跑; 从 queue 摘掉, 进 _running 表
    def finish(task_id: str, *, duration: float) # 任务完成; 记录耗时供 ETA 估算

    def waiting(stage_id: str | None = None) -> list[QueueItem]     # 看队列
    def running() -> list[tuple[str, str]]       # 在跑的 (task_id, stage_id)
```

### priority + FIFO

- `priority` 高的先出（数值大优先）
- 同 priority 内按 `enqueued_at` 严格 FIFO
- 内部存的是排好序的 list；每次 enqueue 重新 sort（任务数 N < 1000 时性能可接受；如需更高量级用 heapq）

### ETA 估算

- 维护每 `stage_id` 历史耗时（最近 20 个样本）
- ETA 用 **P90 延迟**（取最近样本里第 90 分位） — 更稳健，避免被偶发慢调用拉偏
- 初始无样本时 fallback 5 秒
- 估算公式：`ETA = position × P90_duration`（简化：当前在跑那个没扣 elapsed；后续可优化）

### 跟 Pipeline 的集成

本叶子不强制接入 pipeline。如果业务方要让 stage 走 queue：

```python
queue = QueueManager()
item = queue.enqueue("ai-generate-bg", priority=0)

# 调度器（caller 负责）观察 queue + capacity, 决定何时 start
while queue.position(item.task_id) > capacity_left:
    time.sleep(1)
queue.start(item.task_id)

start_ts = time.time()
result = stage_runner.run(stage_spec, args, task_id=item.task_id)
queue.finish(item.task_id, duration=time.time() - start_ts)
```

调度器实现（worker pool / asyncio loop / multiprocessing）是上层工程层的事。本叶子提供数据结构 + 查询 API。

## 5. RateLimiter — token bucket 限流

**用途**：单位时间内限制某 key 的调用频率。常见场景：
- AI API 每秒限 5 次（key=`"openai-api"`）
- 单用户每分钟限 100 次操作（key=`f"user:{user_id}"`）
- 文件上传每秒 10 MB

**为什么 token bucket**：相比"漏桶"，允许短时 burst（积攒的 token 用完即止），更贴合实际负载模式。

### API

```python
class RateLimiter:
    def __init__()                                       # 内部维护 key → bucket 表

    def configure(key: str, *, rate: float, capacity: float) -> None
        # 配某 key 的限流: rate = 每秒补充 token 数; capacity = 桶容量

    def acquire(key: str, n: float = 1.0) -> bool
        # 尝试拿 n 个 token; 拿到返 True, 不够返 False (不等待)

    def wait_for(key: str, n: float = 1.0, *, timeout: float | None = None) -> bool
        # 阻塞等到能拿到 n 个 token; 超时返 False
        # 内部 sleep poll (粗粒度); 真实生产用 condition variable

    def available(key: str) -> float                     # 当前可用 token 数
```

### token bucket 算法

```
每个 key 维护:
    capacity:    桶容量 (max token 数)
    rate:        每秒补充速率
    tokens:      当前 token 数 (浮点)
    last_refill: 上次补充时间

acquire(n):
    now = time.time()
    elapsed = now - last_refill
    tokens = min(capacity, tokens + elapsed * rate)
    last_refill = now
    if tokens >= n:
        tokens -= n
        return True
    return False
```

### 未配置 key 行为

`acquire(unknown_key)` 默认放行（返 True）—— 业务方必须显式 `configure` 才生效。这是为了避免"忘了配置 key 导致全部拒绝"的坑。

## 6. 跟 02-conflict-resolution 的边界

| | concurrency (本叶子) | conflict-resolution |
|---|---|---|
| 关注层 | 访问层（resource 争抢） | 数据层（逻辑冲突） |
| 工具 | Lock / Queue / RateLimiter | 4 策略 resolver |
| 典型场景 | "A 和 B 都想编辑 entity X 这一秒" | "A 和 B 都改了 entity X 的字段 Y，base 是 v=3，谁赢" |
| 调用顺序 | 先这里串行化 / 限流 | 串行化后仍冲突时这里 merge |

简单说：**本叶子让请求不打架，conflict-resolution 让请求结果不打架。**

## 7. 跟 Command / Pipeline / Asset 的集成（推荐 pattern）

本叶子**不强制集成**到任何其他叶子。业务方自己决定哪些场景用：

```python
from lock import AdvisoryLockManager
from queue import QueueManager
from rate import RateLimiter
from command import CommandRegistry

lock_mgr = AdvisoryLockManager()
queue_mgr = QueueManager()
rate_mgr = RateLimiter()
rate_mgr.configure("openai-api", rate=5.0, capacity=10.0)

# 推荐 pattern 1: 编辑前拿锁
def open_editor(actor, entity_type, entity_id):
    try:
        handle = lock_mgr.acquire(
            entity_type=entity_type, entity_id=entity_id,
            user_id=actor.user_id, device_id=actor.device_id,
        )
        # 进入编辑 UI; 显示 handle 给前端做续期
    except ZeError as e:
        if e.code == "acl_denied":
            # 提示用户: "张三正在编辑" + 询问是否 force
            ...

# 推荐 pattern 2: 长任务排队 + 限流
def kickoff_ai_pipeline(args):
    if not rate_mgr.acquire("openai-api"):
        # 速率超限; caller 决定 wait_for 还是返错
        return {"error": "rate_limited"}
    item = queue_mgr.enqueue("ai-generate", priority=0)
    # 调度器 (worker pool) 后续 start + run + finish
```

## 8. 红线规则 + 反模式

红线：

- **AdvisoryLock 不能当作数据保护层** — 真数据保护靠 entity.update 的 CAS（AP-CC1）
- **QueueManager 不调度执行** — 它只是数据结构 + 查询；执行调度（worker pool）是上层工程层（AP-CC2）
- **RateLimiter unknown key 默认放行** — 不配置就不生效；不要依赖"全局默认限流"（AP-CC3）
- **acquire 后必须 release** — 否则 lock 占用到 TTL 过期；用 try/finally
- **RateLimiter 已线程安全** — token bucket 内部加了 `threading.Lock`，多线程共用同 key 安全（被 [06-resource-budget/](../06-resource-budget/) 的 ResourceGate 并发调 `wait_for` 依赖）；**AdvisoryLockManager / QueueManager 仍是单线程假设**，多 worker 共用需外加 lock

反模式：

- **AP-CC1** 业务方把 AdvisoryLock 当真锁用：`acquire → 改数据 → release` 没 CAS → 别的用户绕过 lock 直接 update 仍能改成功，破坏隔离假设
- **AP-CC2** 业务方期待 `queue.start(task_id)` 自动跑 stage → start 只是"我决定从 queue 取出这个任务"；真跑由 caller 调 stage_runner
- **AP-CC3** caller 忘了 `rate_mgr.configure(key, rate, capacity)` 就 `rate_mgr.acquire(key)` → 默认放行，实际等于没限流
- **AP-CC4** acquire 拿了 lock 后崩溃 / 异常退出 → 没 release，锁保留到 TTL；用 try/finally 兜底
- **AP-CC5** force 接管 lock 没记审计 → 后续追责"谁强制接管的"查不到；caller 应在 force=True 时记 log
- **AP-CC6** 把超短任务（毫秒级 command）也塞 queue → queue 是为长任务设计；瞬时操作直接 dispatch
- **AP-CC7** RateLimiter rate 设置过低（rate=0.1）然后 `wait_for` 阻塞主线程 → 应该走异步队列

## 9. Python API

```python
# 完整签名（详细见 §3 / §4 / §5）

from lock import AdvisoryLockManager, LockHandle
from queue import QueueManager, QueueItem
from rate import RateLimiter

# AdvisoryLockManager
lock_mgr = AdvisoryLockManager(ttl_sec=300.0)
handle = lock_mgr.acquire(entity_type="obj", entity_id="e1",
                          user_id="u1", device_id="d1", force=False)
lock_mgr.release(handle)
lock_mgr.state("obj", "e1")                                # → LockHandle | None
lock_mgr.sweep_expired()                                    # → int (被清数量)

# QueueManager
queue_mgr = QueueManager()
item = queue_mgr.enqueue("ai-stage", priority=10)
queue_mgr.position(item.task_id)                            # → int (1-indexed)
queue_mgr.estimate_eta_sec(item.task_id)                    # → float (秒)
queue_mgr.start(item.task_id)                               # → bool
queue_mgr.finish(item.task_id, duration=5.2)
queue_mgr.waiting("ai-stage")                               # → [QueueItem]
queue_mgr.running()                                          # → [(task_id, stage_id)]

# RateLimiter
rate_mgr = RateLimiter()
rate_mgr.configure("openai-api", rate=5.0, capacity=10.0)
rate_mgr.acquire("openai-api", n=1.0)                       # → bool (不阻塞)
rate_mgr.wait_for("openai-api", n=1.0, timeout=30.0)        # → bool (阻塞等; 超时 False)
rate_mgr.available("openai-api")                            # → float (当前 token 数)
```

## 10. mod 端注意

本叶子 mod 端不需要专门模板——业务方在 `02-mod/05-code/commands_register.py` 写 command handler 时按需用这些工具：

```python
# 业务 mod 代码示例
lock_mgr = AdvisoryLockManager()       # framework 启动时构造单例

@registry.command(id="entry.edit-start", label="开始编辑", kind="native", touches=["entry"])
def edit_start(args, actor, source, doc):
    handle = lock_mgr.acquire(
        entity_type="entry", entity_id=args["id"],
        user_id=actor.user_id, device_id=actor.device_id,
        force=args.get("force", False),
    )
    return new_operation(...)   # 产生 op 记录 "u1 打开了 entry/abc 的编辑器"
```

## 11. 当前实装状态

### ✅ 已实装

| 项 | 说明 |
|---|---|
| `LockHandle` frozen dataclass | lock_id / entity / holder / acquired_at / expires_at |
| `AdvisoryLockManager` | acquire / release / state / sweep_expired + TTL + force 接管 |
| `QueueItem` dataclass | task_id / stage_id / priority / enqueued_at |
| `QueueManager` | enqueue / position / estimate_eta_sec / start / finish / waiting / running |
| ETA 基于 P90 历史延迟 | 滑动窗口 20 样本; 无样本 fallback 5 秒 |
| `RateLimiter` (token bucket) | configure / acquire / wait_for / available；**已加 `threading.Lock` 线程安全** |
| 未配置 key 默认放行 | 避免"忘配置导致全拒"的坑 |
| ZeError 共享 | 从 `entity` 模块 import |

### ⏳ 待补（真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| 线程安全：AdvisoryLockManager / QueueManager（多 worker 共用同实例）| 加 threading.Lock；当前单线程可用。**RateLimiter 已加锁线程安全**（见上 ✅ 表）|
| 跨进程 / 跨 server 锁（Redis-based 分布式锁）| 等真有多 server 部署需求 |
| QueueManager 自动调度器（worker pool）| 上层工程层；本叶子只提供数据结构 |
| RateLimiter 异步 wait_for（asyncio condition）| 当前 sleep poll；async 场景 caller 自包 |
| 锁 force 接管的审计落 OpLog | 业务方 caller 自己加 |

### 原则

- **签名先定**：所有 API 签名固定，跨叶子调用不会变
- **不强制集成**：lock / queue / rate 是工具，caller 按需用；不嵌入 command / pipeline 默认路径
- **CAS 是真硬约束**：本叶子的 lock 是 UI 软约束，数据保护仍走 entity.update 的 expected_version
