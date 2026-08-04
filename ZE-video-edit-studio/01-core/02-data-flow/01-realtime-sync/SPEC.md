# Realtime Sync — durable commit 之后的实时分发

## 1. 目标与边界

本叶子负责把一条已经成立的 canonical `entity_change` 送给匹配的订阅者，并提供短时
断线回放。它不决定业务修改是否成功，也不持久化 Document 或 OpLog。

业务目标可以用一句话判断：用户在标签页 A 修改后，标签页 B 收到事件时，这笔修改
必须已经能从持久 Document 读取，并且能在 OpLog 中审计。

### 唯一合法顺序

```text
validate/confirm
      -> execute handler, 得到 Operation 或 MutationBatch
      -> CommitStore.commit(intent)
      -> receipt 证明 Document durable + 整批 OpLog durable
      -> 基于同一提交后的 Document 快照构造 canonical event
      -> EventPublisher.publish
      -> 返回 CommitExecution(result, receipt, events)
```

`CommandRegistry` 只拥有前两步中的校验、确认门控和 handler 执行。旧构造参数
`broker=` 与 dispatch 参数 `op_log=` 仅为迁移期签名兼容，传入后也不能产生副作用。

### 非目标

- `RealtimeBroker` 不是数据库，不是 event journal，也不是跨进程消息总线。
- 内存 replay buffer 不承诺进程重启后的恢复，更不承诺 exactly-once。
- broker 的 `seq` 不是 `document_version`，不能拿来做跨设备 CAS 或云版本比较。
- subscriber callback 只消费事件，不允许回头执行命令或直接写 Document。

## 2. 组件职责

| 组件 | 拥有的责任 | 明确不拥有 |
|---|---|---|
| `CommandRegistry` | source/poll/ACL/确认门控、执行 handler、返回 Operation | save、append、publish |
| `CommitCoordinator` | 串行化一笔 mutation，调用 store，核对 receipt，构造并投递事件 | 具体文件或数据库格式 |
| `CommitStore` | 对一笔 `CommitIntent` 给出可审计 durability receipt | UI 通知与 subscriber |
| `CanonicalEventFactory` | 从 Operation、提交后 Document、receipt 构造唯一 payload | 决定提交是否成功 |
| `EventPublisher` | 投递已经 durable 的 payload，报告 published/pending/failed | 重放业务命令 |
| `RealtimeBroker` | 进程内匹配、分发、短时 replay 与统计 | 业务事务与长期恢复 |
| `Transport` | 把 payload 送到一个具体 subscriber | 决定 topic、提交和版本 |

这个拆分保证文件存储未来换成 SQLite/Postgres、内存 transport 未来换成 SSE/WebSocket
时，命令与事件语义不需要重写。

## 3. RealtimeBroker 协议

```python
class RealtimeBroker:
    def __init__(
        self,
        *,
        replay_buffer_size: int = 256,
        transport: Transport | None = None,
    ) -> None: ...

    def publish_change(self, payload: dict) -> int: ...

    def subscribe(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        doc_id: str | None = None,
        callback: Callable[[dict], None],
        since_seq: int = 0,
    ) -> Subscription: ...

    def stats(self) -> dict: ...
```

### publish

`publish_change` 的进程内步骤：

1. 增加本进程 delivery `seq`；
2. 复制 payload、加入 `seq`，放入固定长度 replay buffer；
3. 根据 payload 的 `changes` 和 `doc_id` 计算匹配订阅者；
4. 在 broker 锁内把同一 payload 放入每个匹配订阅的有序待发队列；
5. 在 broker 锁外由每个订阅唯一的 drainer 调用 transport，避免慢 callback 阻塞
   broker 状态锁，同时保证 replay 与并发 live publish 不会交错成 `11,1..10`；
6. 返回 delivery `seq`。

broker 不修改原 payload，不修改 Document，也不 append OpLog。它信任上游协调器已经完成
协议校验和 durability 判定。

### topics 与过滤

订阅 API 对外不暴露 topic 字符串，但内部等价于：

| 参数 | 匹配范围 |
|---|---|
| 全部为 `None` | 所有事件 |
| `entity_type="entry"` | 所有 entry |
| `entity_type="entry", entity_id="e1"` | 指定 entity |
| `doc_id="doc-abc"` | 指定 Document 的所有 change |

同一 payload 即使包含多个都匹配的 change，对同一 subscription 也只投递一次。

## 4. canonical payload

基础字段沿用 `_protocol/CHANGE-FLOW`：

```python
{
    "type": "entity_change",
    "doc_id": "doc-abc",
    "op_id": "op-uuid",
    "trace_id": "trace-uuid",
    "source": "user" | "ai" | "sync" | "system",
    "command_id": "entry.update",
    "actor": {...},
    "ts": 1700000000.0,
    "changes": [{
        "entity_type": "entry",
        "entity_id": "e1",
        "change_kind": "created" | "updated" | "soft_deleted"
                       | "restored" | "hard_removed",
        "changed_fields": ["title"],
        "patches": [...],
        "snapshot": {...} | None,
    }],
    "commit_id": "commit-uuid",
    "document_version": 42,
}
```

`commit_id` 把 batch 内的多条 Operation 归到同一业务提交；`document_version` 是持久层
确认的文档版本；`trace_id` 在 HTTP/MCP/CLI/pipeline/undo/redo 间保持同一追踪身份。
broker 添加的 `seq` 只表示本实例投递顺序，不能覆盖或替代这些字段。`stats.stream_id`
标识这一段进程内 seq 域；进程重启后即使 project generation 没变，新的 stream id 也会
迫使客户端重取 canonical snapshot，不能把重新从 1 开始的 seq 接在旧 cursor 后面。

## 5. CommitCoordinator 集成

推荐调用形态：

```python
coordinator = CommitCoordinator(
    document=doc,
    store=commit_store,
    publisher=BrokerEventPublisher(broker),
)

execution = coordinator.execute(
    lambda: registry.dispatch(
        command_id, args, actor=actor, source=source, doc=doc,
    ),
    trace_id=trace_id,
)
```

producer 允许返回：

| 返回值 | 持久化 | canonical events |
|---|---|---|
| `None` | 不调用 store | 空 |
| `PendingConfirmation` | 不调用 store | 空 |
| `Operation` | 一笔 commit | 一条 |
| `MutationBatch` | 整批作为一个 commit | 每个 Operation 一条，共用 commit/version |

协调器必须先得到与 intent 身份完全匹配的 receipt，且同时满足：

```text
status == committed
document_durable == true
oplog_durable == true
event_status == not_required
```

才允许构造或发布 canonical event。任何字段不匹配、store 抛出未分类异常、Document
durable 但 OpLog 未确认等情况，都不得 publish。

事件必须从协调器冻结的 `CommitExecution.events` 消费。调用方不能在锁外重新读取可能
已经前进的 Document，再自己调用 `affected_entities` 拼第二份事件。

## 6. 失败语义

业务 durability 与事件 delivery 是两个状态轴：

| 情况 | 业务状态 | 事件状态 | 可以做什么 |
|---|---|---|---|
| Document/OpLog 都 durable，投递成功 | committed | published | 正常返回 |
| 两者 durable，outbox 已接受稍后投递 | committed | pending | 只等待/重试事件 |
| 两者 durable，实时投递失败 | committed | failed | 修复事件，不能重放命令 |
| Document durable，OpLog 结果未知 | indeterminate | not_required | 停止自动重试，进入人工/系统修复 |
| mutation 未 durable 且已回滚 | aborted | not_required | receipt 明确允许时才可重试 |
| 仅内存 mutation 未回滚 | dirty | not_required | 先恢复内存/重载，不能叠加执行 |

最重要的结论是：broker 异常不能让已经 committed 的业务命令重新执行。否则一次短暂
网络故障会把同一个创建、扣减或移动动作做两遍。

## 7. replay 与恢复

`since_seq` 的当前语义：

- `0`：不回放，只接收订阅后的新消息；
- `N > 0`：先回放当前 buffer 中 `seq >= N` 的消息，再接新消息；
- `N == last_seq + 1`：历史恰好追平，只接后续 live 消息；
- `N > last_seq + 1`：cursor 属于另一段/错误的 seq 域，返回 `cursor_ahead` gap；
- `N` 早于 `oldest_buffered_seq`：返回 `replay_buffer_gap`，且**不投递残缺后缀**。

`Subscription` 显式报告 `requested_since_seq / oldest_buffered_seq /
last_seq_at_subscribe / replay_gap / gap_reason`。任何 gap 都不能悄悄假装同步完成，也不能
把 buffer 里碰巧还在的后半段交给客户端。当前 SSE 将它投影为一帧 `resync_required` 后关流，
客户端整份 reload；云化阶段应通过 durable cursor API 拉 journal page，遇到无法补齐的 gap
再拉 snapshot。内存 buffer 只优化短时重连，不承担正确性。

### HTTP snapshot 到 SSE 的闭合规则

`GET /api/document` 在同一个 project commit lock 下同时返回 canonical Document、
`event_cursor` 与 `event_epoch`。浏览器只在这份 snapshot 就绪后订阅：

```text
event_epoch = GET.event_epoch
since_seq   = GET.event_cursor + 1   # inclusive replay
```

SSE 每帧都带 `project_generation + event_epoch`。generation、epoch、seq 或
`document_version` 任一倒退/跳洞，都只能丢弃旧证据并重新 GET，不能局部猜测修补。服务器发现
buffer gap、队列溢出、broker incarnation 变化或项目换代时发送 `resync_required` 后关流；发现
session、工具权限、项目 ACL 或 demo owner 已失效时发送 `access_revoked` 后关流。原生
`EventSource` 的自动重连不能复用旧 URL 冒充恢复，前端必须关闭旧实例，用最新 GET 的 cursor /
epoch 构造新连接。

## 8. 并发与回调规则

- broker 用 `RLock` 保护 subscriptions、buffer、seq 与统计；transport delivery 不持
  broker 自身状态锁。
- replay snapshot、订阅注册与 live enqueue 同处一个 broker 临界区；每个 subscriber 只有
  一个锁外 drainer。慢 callback 可以拖慢自己，不能让后发布帧越过 replay，也不能阻塞别的订阅。
- CommitCoordinator 用共享 mutation lock 保持 commit、event build 与同一提交事件序列
  的顺序；所有写入口必须消费同一个协调器/锁域。
- `publish_change` 检测同线程重入。subscriber callback 中再次 publish 会抛错，避免
  无限递归。
- callback 必须只做渲染、缓存失效或排队，不得同步执行新的 command。需要响应式业务
  动作时，应进入独立队列并形成新的 trace/commit。
- 一个坏 subscriber 的异常由 `InMemoryTransport` 隔离，不影响其他 subscriber；这不
  等于事件投递成功可被忽略，生产 transport 必须把失败反馈给 publisher/outbox。

## 9. 持久化与云化升级

当前 broker 状态全部在内存：subscription、replay buffer 与 seq 在重启后消失。成熟的
跨进程/跨设备版本应增加：

1. 与业务 commit 同事务写入的 durable event/outbox；
2. 每个 project/document 单调持久 cursor，语义与 revision 对齐；
3. `events?after=cursor` 的分页 catch-up 与 snapshot API；
4. SSE/WebSocket 只做 journal tail，不自己发明另一套版本；
5. 客户端持久 last-applied cursor，明确 gap、duplicate 与 reconnect 规则；
6. presence/光标等临时协作信息走独立 TTL 通道，不进入业务 revision/OpLog。

多进程 publisher 需要 outbox claim/ack 或等价机制；不能让每个进程使用各自 broker seq
冒充全局顺序。

## 10. 审计门

| Gate | 名称 | 检查什么 |
|---|---|---|
| `GATE-RT-01` | broker_publish_routes_to_subscribers | publish 能到达订阅者 |
| `GATE-RT-02` | subscribe_entity_type_filter | entity 过滤正确 |
| `GATE-RT-03` | subscribe_since_seq_replay | replay 窗口与顺序正确 |
| `GATE-RT-04` | unsubscribe_stops_delivery | 取消后不再收到 |
| `GATE-RT-05` | payload_format_entity_change | 基础 payload 字段完整 |
| `GATE-RT-06` | cmd_dispatch_has_no_commit_side_effects | registry 不 append、不 publish |

RT-06 是防回退门：任何人把 `_broker.publish_change` 或 `op_log.append` 再塞回 dispatch，
都应立即失败。durable-before-publish 的跨叶子行为由本叶子的 coordinator 集成测试覆盖。

## 11. 红线与反模式

- **R1**：handler 或 CommandRegistry 直接 publish。此时没有统一 durability 证据。
- **R2**：Document save、OpLog append、publish 由 HTTP/MCP/CLI 各写一遍。不同入口必然漂移。
- **R3**：收到 publisher 错误就重放业务命令。业务可能早已 durable，只是事件没送到。
- **R4**：把 broker seq 当 document revision。重启或多进程后会倒退/分叉。
- **R5**：buffer gap 仍返回“已同步”。应 durable catch-up 或 snapshot reload。
- **R6**：从 Operation 在各 surface 重建 payload。canonical event 只能由协调器构造一次。
- **R7**：subscriber callback 同步执行 command。会产生重入、乱序和难以审计的隐式写链。
- **R8**：把进程内 broker 描述成离线/跨设备完成态。当前 transport 能力必须如实暴露。
- **R9**：只检查 generation、不检查 event epoch。进程重启后同代 seq 从 1 开始，会把旧 cursor
  永久卡死或把新事件当 duplicate。
- **R10**：buffer gap 时仍投递可见后缀。客户端会把缺失历史误认为一段完整连续事件。

## 12. 实装状态

### 已实装

- RealtimeBroker、Subscription、过滤、短时 replay、显式 gap、stream incarnation、
  replay/live catch-up barrier、stats、线程安全保护；
- InMemoryTransport 与 WSTransport skeleton；
- HTTP snapshot cursor/epoch、SSE generation/epoch fence、gap/overflow/resync/access-revoked
  控制帧，以及浏览器动态重连；
- CommandRegistry 纯执行边界；
- CommitCoordinator 的 durable receipt gate；
- CanonicalEventFactory 与 BrokerEventPublisher；
- committed / indeterminate / event failed 的集成测试。

### 后续阶段

- durable commit journal/outbox；
- Postgres/SQLite transaction store；
- 持久 cursor、snapshot 与 gap recovery；
- SSE/WebSocket 跨设备 transport；
- 多进程 outbox worker、可观测 delivery lag 与修复队列。

这些升级改变存储和 transport，不改变本 SPEC 的核心合同：canonical 实时事件永远排在
可证明的业务 durability 之后。
