# 05-concurrency — 并发访问控制

> 一句话：**访问层串行化 / 排队 / 限流**。三个独立工具：编辑锁（互斥）、任务队列（排队 + ETA）、token bucket 限流。它们让并发请求不打架；数据层冲突由 [02-conflict-resolution/](../02-conflict-resolution/) 负责。

## 在框架里的位置

- 上一级：[02-data-flow/](../) — 数据流 5 个叶子
- 同级：[01-realtime-sync/](../01-realtime-sync/) / [02-conflict-resolution/](../02-conflict-resolution/) / [03-offline-first/](../03-offline-first/) / [04-history-rollback/](../04-history-rollback/)
- 跟数据层 CAS 的关系：advisory lock 是 UI 软约束；真数据保护仍走 `entity.update` 的 `expected_version`

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览）|
| [SPEC.md](./SPEC.md) | 11 节完整规则（lock / queue / rate / 边界 / 集成 pattern / 反模式）|
| [lock.py](./lock.py) | AdvisoryLockManager + LockHandle |
| [queue.py](./queue.py) | QueueManager + QueueItem |
| [rate.py](./rate.py) | RateLimiter（token bucket）|
| [test_concurrency.py](./test_concurrency.py) | pytest 测试（41 个）|

## 当前状态

**✅ 已实装**（全部无跨叶子依赖项）

- `AdvisoryLockManager` — acquire / release / state / sweep_expired + TTL 过期 + force 接管 + 同 user+device 续期
- `LockHandle` frozen — lock_id 防别人 release 你的锁
- `QueueManager` — enqueue / position / estimate_eta_sec / start / finish / waiting / running
- 按 stage 分组 + priority 优先 + 同 priority FIFO
- ETA 基于历史 P90 延迟（20 样本滑动窗口；无样本 fallback 5 秒）
- `RateLimiter`（token bucket）— configure / acquire / wait_for / available
- 未配置 key 默认放行（避免"忘配置导致全拒"）

**⏳ 待补**（真有跨叶子依赖）

- 线程安全（多 worker 共用同 manager 实例）— 加 threading.Lock 装饰，当前单线程可用
- 跨进程 / 跨 server 锁（Redis-based 分布式锁）— 等真有多 server 部署需求
- QueueManager 自动调度器（worker pool）— 上层工程层；本叶子只提供数据结构
- RateLimiter 异步 wait_for（asyncio condition）— 当前 sleep poll；async 场景 caller 自包

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态)。

## 怎么用

### AdvisoryLock — 编辑互斥

```python
from lock import AdvisoryLockManager

lock_mgr = AdvisoryLockManager(ttl_sec=300.0)

# u1 点"编辑"
h = lock_mgr.acquire(entity_type="entry", entity_id="abc",
                     user_id="u1", device_id="d1")
# UI 进入编辑器; 周期性续期保持 lock 活

# u2 同时点 → 抛 acl_denied 含 locked_by_user="u1"
# 前端展示"u1 正在编辑, 是否强制接管?"

# u2 force 接管 (管理员场景, caller 应记审计)
h2 = lock_mgr.acquire(entity_type="entry", entity_id="abc",
                      user_id="u2", force=True)

# 退出编辑器
lock_mgr.release(h2)
```

### QueueManager — 长任务排队

```python
from queue import QueueManager

q = QueueManager()

# 入队 (caller 调度器决定何时 start)
item = q.enqueue("ai-generate", priority=0)
print(f"位置 {q.position(item.task_id)}, ETA {q.estimate_eta_sec(item.task_id):.1f}s")

# 调度器从队首取
while q.position(item.task_id) > 1:
    time.sleep(1)
q.start(item.task_id)

# 跑 stage
start = time.time()
result = stage_runner.run(spec, args)
q.finish(item.task_id, duration=time.time() - start)
```

### RateLimiter — token bucket 限流

```python
from rate import RateLimiter

rl = RateLimiter()
rl.configure("openai-api", rate=5.0, capacity=10.0)  # 每秒 5 个, 桶容 10

# 不阻塞: 拿不到立即返 False
if rl.acquire("openai-api"):
    call_openai_api()
else:
    return {"error": "rate_limited"}

# 阻塞等
if rl.wait_for("openai-api", timeout=30.0):
    call_openai_api()
else:
    return {"error": "timeout"}
```

## 设计要点

- **AdvisoryLock 是 advisory（软）锁** — UI 层用；真数据保护靠 entity.update 的 CAS
- **同 user+device 续期不冲突** — 同一会话重复 acquire 等同 keep-alive
- **force 接管由 caller 记审计** — framework 不强制；管理员场景应该 log
- **QueueManager 不调度** — 它只是数据结构 + 查询；真跑由 caller 决定（worker pool）
- **ETA 用 P90 不用 mean** — 抗偶发慢调用的偏置
- **RateLimiter 未配置默认放行** — 避免"忘配置导致全部拒绝"的坑

## 跟其他叶子的关系（不强制集成）

| 集成点 | 建议 pattern |
|---|---|
| Command 改 entity 前拿 lock | mod 端写 `entry.edit-start` command 在 handler 里 `lock_mgr.acquire(...)` |
| Pipeline stage 走队列 | caller 调度器从 queue 出队 → start → stage_runner.run → finish |
| AI / API 调用走限流 | command handler 调外部 API 前 `rate.acquire(...)` |

本叶子**不嵌入**这些路径，只提供工具。

## 跑测试

```bash
cd 01-core/02-data-flow/05-concurrency/
python -m pytest test_concurrency.py -v
```
