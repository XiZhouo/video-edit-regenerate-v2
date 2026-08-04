# 提交流程合同（Commit Flow）

> 本文把「一次编辑什么时候才算保存成功」说死。W03 已将 HTTP、MCP、CLI、Pipeline、undo/redo 收口到同一个 project-scoped `CommitCoordinator`；W04 已补齐提交前完整内存回滚；W06 起替换存储实现，不再改变调用形状。

## 1. 我们要解决的不是“有没有调用 save”

编辑器一次操作会依次碰到三种不同的事实：内存里的文档变了、文档已经可靠落盘、这次变化已经写进操作日志并通知其他客户端。过去各入口自己排列这些动作，最危险的情况是页面告诉用户“成功”，但实际上只完成了其中一部分。

V3 的统一顺序是：

```text
validate → apply → document durable → oplog durable → event
```

1. `validate`：鉴权、参数、CAS 和业务约束全部通过；此时不能有数据变化。
2. `apply`：在可回滚的工作单元里应用变更，形成一条 Operation 或一组有序 Operations。
3. `document`：把业务文档写入可靠存储，并确认耐久。
4. `oplog`：把同一个 Operation 写入耐久操作日志。
5. `event`：把已提交变化放进 outbox 或发布给订阅者。
6. `after-commit action`：事件已被接受后，才允许启动线程、写运行态 manifest 或控制 pipeline；失败单独记账，不能重放业务命令。

只有文档和操作日志都耐久，业务提交才成立。事件是已提交事实的传播，不是业务提交本身。

`apply` 发生在嵌套 mutation session 中：CommandRegistry 的内层 session 处理单个 handler 半途失败，CommitCoordinator 的外层 session 处理多条命令组成的一笔提交。只有存储明确证明 Document 与 OpLog 都未写入，外层才恢复快照；任何 unknown 或 durable 结果都保留当前内存图并进入对账，不能为了“看起来干净”制造假回滚。

## 2. 保存策略只有自动策略

`SavePolicy` 不表达 SQLite、PostgreSQL、单进程或多进程；这些是部署拓扑。它也没有“manual save”。

| 策略 | 含义 | 使用边界 |
|---|---|---|
| `auto_commit` | 每次用户可见编辑都由框架自动提交 | 普通编辑器的目标默认值 |
| `checkpoint_commit` | 长任务由框架在声明好的检查点自动提交 | 批处理、管线和可恢复长任务；不是让用户点保存 |
| `legacy` | 旧入口迁移期的兼容标记 | 新模块禁止使用；不能据此承诺云编辑保存能力 |

“新建版本”“发布”“另存为”“导出”仍是独立业务动作，不能冒充保存策略。

## 3. 四种业务提交状态

| 状态 | 人话解释 | 能否自动重放整笔命令 |
|---|---|---|
| `committed` | 文档和 OpLog 都已经耐久 | 否；再执行会重复改数据 |
| `dirty` | 只改了当前进程内存，既未落盘也没成功回滚 | 否；必须先回滚或重载文档 |
| `aborted` | 已确认没有任何耐久变化，内存也干净 | 是；“安全”只表示不会重复写，不表示错误一定会自行消失 |
| `indeterminate` | 已跨过部分耐久边界，无法把整笔操作证明为成功或失败 | 否；必须用 commit/operation id 对账修复 |

`retry_safe` 专指“能否自动重放整笔业务命令”。事件投递有自己的重试队列；重试事件绝不能重新执行业务命令。

## 4. 失败不能只看异常发生在哪一行

同样一条 I/O 异常，发生在写入之前和写入确认丢失之后，含义完全不同。因此映射必须同时接收阶段与已经确认的耐久事实。Document 与 OpLog 的写入结果都是三态：`not_durable`（确认未写）、`durable`（确认已写）、`unknown`（结果无法确认）。任何 `unknown` 都进入 `indeterminate`，绝不能当普通失败自动重放。

| 已确认事实 | 结果 | 原因与处理 |
|---|---|---|
| 校验失败，或应用失败后完整回滚 | `aborted`，`retry_safe=true` | 没有留下变化，可由上层按错误类型决定是否重试 |
| 内存已应用但回滚失败，文档未耐久 | `dirty`，不可自动重试 | 先重载或人工修复当前工作副本 |
| 文档尚未耐久且回滚成功 | `aborted`，可安全重试 | 已证明磁盘和内存都没有留下本次变化 |
| 文档已耐久、OpLog 未耐久 | `indeterminate`，不可自动重试 | 业务数据可能已经变了；重放会二次修改，必须对账 |
| 文档与 OpLog 都耐久，事件发布失败 | 仍为 `committed` | outbox 已接管则事件为 `pending`；没有可重试载体则为 `failed` |
| OpLog 已耐久但确认响应丢失 | `committed`，不可重放 | 服务端应按 id 返回既有 receipt，而不是再执行命令 |

最需要守住的边界是：**文档耐久但 OpLog 未耐久绝不叫普通失败，更不能自动重试。**

## 5. EventStatus 为什么单列

`EventStatus` 有四态：

- `not_required`：该提交不需要传播，或尚未进入事件阶段。
- `pending`：提交已经成立，事件在耐久 outbox 中等待投递或重投。
- `published`：订阅者通道已经接受事件。
- `failed`：提交已经成立，但事件没有进入可自动恢复的投递路径，需要告警或修复。

所以“别人暂时没看到更新”不能让服务端撤销已经保存的文档，也不能让客户端把同一个编辑再发一遍。

## 6. Receipt 是机器可校验的事实，不是提示语

`CommitReceipt` 使用固定版本 `ze.commit-receipt/1`，始终输出完整键集：提交与文档标识、一到多条 Operation 标识、文档版本、Document/OpLog 耐久确认、trace、保存策略、业务状态、事件状态、失败阶段、`retry_safe` 和失败事实。批量撤销、重做和管线提交因此不必伪装成单 Operation。JSON 使用固定枚举值与规范化排序，可落审计日志、跨进程传输并做字节级快照测试。

失败 receipt 的 `status`、`event_status` 和 `retry_safe` 不能由调用方随意填写；它们必须由 `CommitFailure + DurabilityFacts` 推导。反序列化时如果有人修改了推导字段、漏字段或塞入未知字段，合同会拒绝该 payload。

## 7. 已落地的统一入口

- `CommandRegistry` 只执行 handler，不再 append OpLog 或发布 canonical event。
- `CommandMutation` 把主命令、占位等派生 Operation 合成一个 commit id / receipt。
- producer 内递归 `execute()` 会在 store write 前失败，不能偷偷形成第二笔提交。
- `DeferredAction` 只有在业务双耐久且 event 被 publisher 接受后运行；`ActionReceipt` 与业务 receipt 分开。
- `FileCommitStore` 是诚实的兼容 adapter：固定 Document→OpLog，但跨文件失败仍可能 `indeterminate`。
- `DocumentMutationSession` 原位恢复 Document、bucket 与 Entity 身份；回滚失败输出稳定 `mutation_rollback_failed`，不会伪装成普通可重试错误。

## 8. 后续实现必须遵守的边界

- HTTP、MCP、undo/redo、管线和系统迁移只能调用同一个 coordinator。
- handler 可以按既有模型先改共享 Document 再形成 Operation，但整个过程必须处在 mutation session 内；producer 内的外部 I/O 仍不受该快照保护，必须迁到 effect/action 边界。
- storage port 必须明确返回“未耐久、已耐久或无法确认”，不能只抛一条普通异常。
- 待确认 token 不能在 producer 开始前消费。它先取得唯一 reservation，再严格按本页 receipt
  分类：committed 消费、已回滚 aborted 释放、dirty/indeterminate 冻结；事件失败不推翻已提交
  业务事实，因此同样消费 token。
- 生产多进程路径应把 OpLog 与 outbox 放进同一数据库事务；文档大对象可用版本指针衔接对象存储。
- `legacy` 只用于过渡和观测。任何新编辑器如果仍依赖它，都不能通过顶级验收。
