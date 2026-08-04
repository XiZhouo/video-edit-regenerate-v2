# 01-realtime-sync — Realtime Sync

> 一句话：实时通知只能描述**已经提交成功**的事实。命令执行器只产出
> `Operation`；`CommitCoordinator` 先确认 Document 与 OpLog 都 durable，再构造并
> 发布 canonical `entity_change`。

## 在框架里的位置

- 上一级：[02-data-flow/](../) — 数据流转层
- 提交边界：[07-persistence/](../07-persistence/) — `CommitStore`、
  `CommitCoordinator` 与稳定的 `CommitReceipt`
- 协议依赖：[_protocol/](../../_protocol/) — `affected_entities` 与
  `to_broadcast_payload`
- 命令来源：[01-command/](../../03-commands-operations/01-command/) — 只校验、
  确认和执行，不保存、不广播

完整路径只有一条：

```text
CommandRegistry -> Operation -> CommitCoordinator -> CommitStore
                                              |       Document durable
                                              |       OpLog durable
                                              v
                                     canonical entity_change
                                              |
                                      EventPublisher / Broker
```

这条顺序解决的是一个很实际的问题：别的标签页收到“修改成功”后，不能重新加载却
找不到文档，或在审计日志里查不到对应操作。

## 本叶子负责什么

| 文件 | 责任 |
|---|---|
| [broker.py](./broker.py) | 进程内 topic-based pub/sub、过滤、有序 replay/live barrier、显式 gap 与订阅统计 |
| [transport.py](./transport.py) | `Transport` 协议、`InMemoryTransport` 与远程 transport 占位 |
| [test_realtime.py](./test_realtime.py) | broker 语义与 durable-before-publish 提交边界 |
| [SPEC.md](./SPEC.md) | 完整协议、失败语义、并发规则与升级方向 |

`CommitCoordinator`、`CanonicalEventFactory` 和 `BrokerEventPublisher` 位于
[07-persistence/](../07-persistence/)，因为“何时允许发布”属于提交事务，而不是
broker 自己可以决定的事。

## 当前已经做到

- `RealtimeBroker(replay_buffer_size=256, transport=None)`：
  - `publish_change(payload) -> seq`
  - 按 `entity_type`、`entity_id`、`doc_id` 过滤
  - `subscribe(..., since_seq=N)` 回放仍在内存窗口中的消息；窗口断裂或 cursor
    属于别的 seq 域时显式报告 gap，不交付残缺后缀
  - `Subscription.unsubscribe()` 与 context manager
  - replay 与并发 live publish 先进入每订阅有序队列，再由唯一 drainer 在锁外投递
  - 每次启动随机 `stream_id`，避免同 generation 下把重启后的 seq=1 接到旧 cursor
  - 多线程 publish / subscribe 的进程内锁保护
- `CommandRegistry` 即使收到旧 `broker` / `op_log` 参数，也不会 append 或发布。
- `CommitCoordinator` 只在 receipt 同时证明 `document_durable=true` 与
  `oplog_durable=true` 后构造 canonical event。
- broker 投递失败不会把已经 durable 的业务提交改判为失败；receipt 会保留
  `status=committed`，并单独标记 `event_status=failed`，等待修复投递。
- payload 除既有 change 字段外，还携带 `commit_id`、`document_version` 和稳定的
  `trace_id`，可以追到同一笔提交。
- Web 接线由 `GET /api/document` 同快照返回 `event_cursor + event_epoch`；SSE 使用
  `since_seq=cursor+1` 做 inclusive replay，每帧校验 project generation、event epoch、
  document version 与 seq。gap、溢出、换代或权限撤销会发控制帧并关闭旧流，浏览器重取
  snapshot 后用新 URL 连接。

## 当前还没有做到

- broker 的 `seq` 只是单进程、单次启动内的投递序号，不是云端文档版本，也不能作为
  跨进程一致性游标。
- replay buffer 在进程重启后会消失，窗口溢出也会丢老消息；它不能代替数据库 journal
  或 snapshot API。
- 当前 SSE 是可用的同进程跨标签 transport，但 `WSTransport` 仍是占位；真正无 sticky
  多进程、离线与跨设备同步需要 durable outbox、持久游标与 snapshot/catch-up API。

因此当前能力是“同一进程内、可检测断档的提交后实时通知”，不是已经完成的云协同协议。
后续扩展 transport 时，不改变本页的核心红线：**未 durable 的事实永远不能先广播；
无法证明连续的事件永远不能冒充已同步**。

## 怎么用

```python
from broker import RealtimeBroker
from commit_coordinator import BrokerEventPublisher, CommitCoordinator
from file_store import FileCommitStore

broker = RealtimeBroker()
store = FileCommitStore(document_path, op_log)
coordinator = CommitCoordinator(
    document=doc,
    store=store,
    publisher=BrokerEventPublisher(broker),
)

broker.subscribe(
    doc_id=doc.id,
    callback=lambda event: render_committed_change(event),
)

execution = coordinator.execute(
    lambda: registry.dispatch(
        "entry.update",
        {"id": "e1", "title": "new"},
        actor=actor,
        source="user",
        doc=doc,
    ),
    trace_id=request_trace_id,
)
```

调用方消费 `execution.receipt` 判断业务提交状态，消费 `execution.events` 做投递或审计；
不能再从返回的 `Operation` 自己拼另一份 payload，也不能在 handler 里直接 publish。

## 五条关键规则

1. `CommandRegistry` 是纯执行器，不拥有 Document save、OpLog append 或 canonical publish。
2. `CommitStore.commit(intent)` 是协调器唯一认可的 durability 证据；仅仅“函数没抛错”
   或“内存里有 Operation”都不算提交成功。
3. canonical event 只能在 Document 与整批 Operation 都 durable 后构造和发布。
4. 业务提交失败与事件投递失败分开表达；投递失败只能重试事件，不能重放业务命令。
5. broker `seq + stream_id` 只服务短时进程内回放；跨设备恢复必须使用持久版本与
   snapshot/cursor。gap 一律重取，不消费残缺后缀。

## 跟 audit gate 的关系

| Gate | 验证 |
|---|---|
| `GATE-RT-01` | `publish_change` 能送达订阅者 |
| `GATE-RT-02` | entity 过滤生效 |
| `GATE-RT-03` | `since_seq` replay 行为正确 |
| `GATE-RT-04` | unsubscribe 后停止接收 |
| `GATE-RT-05` | payload 符合 `entity_change` 基础协议 |
| `GATE-RT-06` | `CommandRegistry` 不再 append 或 publish，提交副作用不会回流 |

## 验证

```bash
cd 01-core/02-data-flow/01-realtime-sync
python -m pytest test_realtime.py -v

python ../../_protocol/audit_gates.py --leaf RT
```
