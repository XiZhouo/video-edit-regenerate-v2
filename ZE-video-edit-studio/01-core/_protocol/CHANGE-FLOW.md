# 数据变更全链路 — Change Flow Protocol

> 跨叶子顶层 SPEC。从"业务方触发变更"到"用户在浏览器看到结果"的完整契约。每条规则对应 [AUDIT-GATES.md](./AUDIT-GATES.md) 一个 gate。

## 1. 这是什么

各业务叶子（document / entity / asset / operation / command / pipeline / concurrency / conflict-resolution）的 SPEC 各管一段；本文档**串联起来**——

- 全链路时序：业务调用 → 服务端处理 → 落盘 → broadcast → 前端精确重渲染
- 各层职责边界
- 异常分支（哪条规则违反抛什么错）
- 前端 entity 级精确重渲染协议
- 审计 gate 索引

业务方写新 command / 新 skill 时**按本文档 pattern 走**；framework 维护者按本文档对应的 gate 跑 [audit_gates.py](./audit_gates.py) 校验。

## 2. 完整时序图

```
[业务调用 / UI 触发]
   ↓
1. (可选) 拿编辑锁 — 05-concurrency
      lock_mgr.acquire(entity_type, entity_id, user_id, device_id)
      └─ 失败 → ZeError("acl_denied", locked_by_user=...)        [GATE-CC-01]
   ↓
2. (可选) 过资源闸门 — 06-resource-budget / 05-concurrency
      重资源(GPU计算/显存/内存/磁盘/网络IO/外部API…): with registry.get(scope).acquire()  (速率门+并发门串联+自证)  [GATE-RB-04]
      └─ 拿不到 → 按 on_violation: wait 等 / reject|cancel_retry 抛 resource_unavailable
      纯速率限流(非重资源): rate_mgr.acquire("openai-api")
      └─ false → caller 决定 wait_for 或返错                      [GATE-CC-03]
   ↓
3. (可选) 进队 — 05-concurrency  (长任务才用)
      queue_mgr.enqueue("stage-id", priority=N)
      └─ 调度器观察 position, 决定何时 start                       [GATE-CC-02]
   ↓
4. 命令 dispatch — 01-command
      cmd_registry.dispatch(cmd_id, args, *, actor, source, doc, op_log)
      ├─ a. self.get(cmd_id)        失败 → unknown_command         [GATE-CMD-01]
      ├─ b. source ∈ sources_allowed  失败 → acl_denied             [GATE-CMD-02]
      ├─ c. spec.poll(args)         失败 → poll_failed              [GATE-CMD-03]
      └─ d. spec.execute(args, actor, source, doc)                  [GATE-CMD-04]
   ↓
5. execute 内部 (业务方写的 handler)
      ├─ 调 entity / doc 修改数据 (framework 内部可用 API)
      │   ├─ entity.update(patches, expected_version=, actor=)
      │   │   ├─ CAS 校验 expected_version  失败 → cas_version_mismatch  [GATE-ENT-01]
      │   │   ├─ read_only 检查             失败 → read_only_violation   [GATE-ENT-02]
      │   │   ├─ 软删检查                   失败 → entity_soft_deleted   [GATE-ENT-03]
      │   │   ├─ (可选) validator 校验 schema  失败 → schema_violation
      │   │   ├─ apply patches; version += 1
      │   │   └─ actor_log 追加 {version, actor, ts, fields}
      │   ├─ (业务方可选) resolve 冲突 — 02-conflict-resolution
      │   │   resolved = resolve(strategy, base, current, incoming)     [GATE-CONF-01]
      │   │   再调 entity.update(resolved)
      │   └─ asset producer 边界 — 03-asset
      │       禁 put_file/promote/add_ref/release/reconcile/gc；仅允许
      │       put(bytes) 准备不可变内容，失败最多留下可回收 orphan
      └─ 造 Operation 返回
          new_operation(command_id, source, actor, patches,
                        reverse_patches, group_id, trace_id, kind)       [GATE-OP-01]
   ↓
6. dispatch 后处理
      ├─ isinstance(op, Operation) 校验   失败 → validation_failed       [GATE-CMD-05]
      ├─ op_log.append(op) (如有 op_log)
      │   ├─ 算 self_hash = sha256(prev_hash + json(op, sort_keys))
      │   └─ entry = {op, prev_hash, self_hash}                          [GATE-OP-02]
      └─ return op
   ↓
7. (可选) 释放编辑锁
      lock_mgr.release(handle)                                            [GATE-CC-04]
   ↓
8. doc.save(path, expected_version=N) — 01-document
      ├─ 409 冲突检测: expected_version != _v → version_conflict          [GATE-DOC-01]
      ├─ `.ze-document.lock` EX 有界等待；锁内 strict 读取 durable head
      ├─ disk `_v` != memory `_v` → 零 generation 写入的 version_conflict [GATE-DOC-08]
      ├─ identity → domain-separated + uint64 length-framed SHA-256
      ├─ 安全 dirfd 写/fsync 完整 entities/.ze-store-v2/<new_v>/ generation
      ├─ 随机 O_EXCL temp → rename document.json（唯一权威 commit point）
      ├─ root fsync；rename 后失败 → document_durability_unknown，内存 _v 对齐 visible head
      ├─ commit 后 best-effort 清旧 generation / legacy raw tree
      └─ collections → generation → payload exact identity 一致性           [GATE-DOC-02/07]
   ↓
9. (可选) queue.finish(task_id, duration)
   ↓
10. (可选,已提交后) AssetStore.gc(canonical_doc)
      ├─ scan_document_ref_counts(doc)（含软删/read_only/重复槽位）
      ├─ reconcile_document_refs(doc) 重建本地 meta.refs
      └─ 只删除不可达本地 blob；无 doc/扫描失败一律不删                    [GATE-AST-02]
   ↓
═════════════════════════════════════════════════════════════════════
后端链路结束 ✅   →   前端链路开始
═════════════════════════════════════════════════════════════════════
   ↓
11. 聚合 entity 级变更 — _protocol/change_set.py
      changes = affected_entities(op, doc) -> list[EntityChange]
      ├─ 按 (entity_type, entity_id) 聚合 patches
      └─ 推导 change_kind (created / updated / soft_deleted / restored / hard_removed)  [GATE-PROTO-01]
   ↓
12. broadcast 消息 (⏳ 等 01-realtime-sync)
      payload = to_broadcast_payload(changes)
      = {
          "type": "entity_change",
          "doc_id": ..., "op_id": ..., "trace_id": ...,
          "changes": [<EntityChange JSON>, ...]
        }
      └─ 推到 channel (WebSocket / SSE / IPC)                            [GATE-PROTO-02]
   ↓
13. 前端订阅 (⏳ 等 04-user-facing)
      subscribe_entity_changes(entity_type=..., entity_id=..., callback)
      ├─ 收到 EntityChange
      └─ 找到 entity_id 对应的渲染单元
   ↓
14. 前端精确重渲染
      渲染单元 key = (entity_type, entity_id)
      ├─ created  → mount 新组件                                          [GATE-PROTO-03]
      ├─ updated  → 只 re-render 该组件 (其他不动)
      ├─ soft_deleted / restored → 该组件标灰 / 恢复
      └─ hard_removed → unmount 组件
   ↓
[用户看到结果 ✅]
```

## 3. 各层职责边界

| 层 | 叶子 | 职责 | 不该做 |
|---|---|---|---|
| 访问层 | 05-concurrency | Lock / Queue / RateLimiter；让请求不打架 | 数据合并 / 业务逻辑 |
| 治理层 | 06-resource-budget | 重资源唯一入口闸门 + 全局预算合同 + 乘法审计 + 运行时自证 | 数据变更 / 调度（沿用 QueueManager）|
| 命令层 | 01-command | CommandSpec / dispatch / ACL；统一变更入口 | 直接 mutate 数据（execute 内部调底层 API）|
| 数据层（聚合）| 01-document | Document API + collections + save/open | 字段级 CAS（那是 entity 的事）|
| 数据层（单元）| 02-entity | Entity + CAS + 软删 + actor_log | 跨 entity 协调（那是 document 的事）|
| 数据层（资产）| 03-asset | sha256 内容寻址 + 引用计数 | 自动接 Document（caller 显式调）|
| 审计层 | 02-operation | Patch + Operation + OpLog hash chain | 决定何时落（caller 在 dispatch 时落）|
| 调度层 | 03-pipeline | Stage / Pipeline / fan_out / retry | 数据变更（在 stage handler 里走 command）|
| 冲突层 | 02-conflict-resolution | 4 策略 resolver 纯函数 | 主动调用（caller 在 handler 里按需调）|
| 协议层 | _protocol（本文档）| 跨叶子契约 + entity 级聚合 + 前端协议 + audit gate | 业务逻辑（只定契约）|

## 4. 前端 entity 级精确重渲染协议

**核心约定**：**渲染单元的 key = `(entity_type, entity_id)`**。前端组件以此作 React key / DOM id / Vue ref。变更通知按此 key 分发到对应组件，组件按需 re-render。

### 4.1 为什么 entity 级而不是字段级

| 维度 | 字段级 | entity 级 |
|---|---|---|
| 一个 op 改同 entity 的 N 字段 | 收 N 个通知 → N 次 re-render 风险 | 收 1 个通知 → 1 次 re-render |
| broadcast 消息体积 | N 个 patch | 1 个 EntityChange 含字段清单 |
| 前端组件粒度 | 字段级组件（不现实） | entity 级组件（自然）|
| 调试复杂度 | 高（追每个字段）| 低（按 entity_id 看动态）|

字段级 patches 仍**保留**在 EntityChange 里——前端如果想做更细粒度优化（只 update 某 div），可以查 `changed_fields`。但默认契约是 entity 级重渲染。

### 4.2 EntityChange 数据结构

```python
@dataclass(frozen=True)
class EntityChange:
    entity_type: str
    entity_id: str
    change_kind: str               # created | updated | soft_deleted | restored | hard_removed
    changed_fields: tuple[str, ...]  # data 字段名; 元操作时 ["__entity__"]
    patches: tuple[Patch, ...]      # 字段级 patches (前端可选用)
    snapshot: dict | None           # entity after 状态; hard_removed 时为 None
```

**字段语义**：

- `change_kind` 五态对应前端 5 种动作（见 §4.3）
- `changed_fields` 标识"哪些字段变了"——前端如果想做"标黄改过的字段"等微交互可用
- `patches` 保留字段级原始变更，给需要的前端用
- `snapshot` 是 entity 当前状态（after），前端用它**直接渲染**，不需要单独查 `doc.get(...)`

### 4.3 五种 change_kind 对应前端动作

| change_kind | 触发 op | 前端动作 |
|---|---|---|
| `created` | `entity.add` 内置 command | mount 新组件，按 snapshot 渲染初始内容 |
| `updated` | `entity.update` 改字段 | 找 (type, id) 对应组件 → 用 snapshot 重渲染（其他组件不动）|
| `soft_deleted` | `entity.delete` 软删 | 组件标灰 / 隐藏 / 划线（业务决定）|
| `restored` | `entity.restore` 恢复 | 组件恢复正常显示 |
| `hard_removed` | `entity.hard_remove` / `cascade_delete` | unmount 组件，从父容器移除 |

### 4.4 推导规则（_protocol/change_set.py 实现）

从 `Operation.patches` 推 `change_kind`：

```
按 (entity_type, entity_id) groupby patches:
  对每组 patches:
    如果有 op="create" 的 patch        → change_kind = "created"
    elif 有 op="delete" 的 patch        → change_kind = "hard_removed"
    elif 有 op="soft_delete" 的 patch   → change_kind = "soft_deleted"
    elif 有 op="restore" 的 patch       → change_kind = "restored"
    else (全是 op="set"/"name")         → change_kind = "updated"

  changed_fields = 该组所有 patch 的 field 集合 (去重 sorted)
  snapshot = doc.find(entity_type, entity_id).to_dict() if not hard_removed else None
```

### 4.5 broadcast payload 格式（消息契约）

```json
{
  "type": "entity_change",
  "doc_id": "doc-uuid-hex",
  "op_id": "op-uuid-hex",
  "trace_id": "trace-uuid-hex",
  "source": "user",
  "actor": {"user_id": "u1", "device_id": "d1", "source": "user"},
  "ts": 1716123456.789,
  "changes": [
    {
      "entity_type": "entry",
      "entity_id": "e1",
      "change_kind": "updated",
      "changed_fields": ["body", "title"],
      "patches": [...],
      "snapshot": {"id": "e1", "type": "entry", "data": {...}, "version": 5, ...}
    },
    {...}
  ]
}
```

`type=entity_change` 是约定 message kind，前端按 type 路由处理器（未来可能有 `cursor_move` / `presence_update` 等其他 message type，目前只 entity_change 一个）。

### 4.6 前端订阅 API 形态（签名先定，实现等 04-user-facing）

```typescript
interface Subscription {
  unsubscribe(): void;
}

function subscribeEntityChanges(opts: {
  entityType?: string;      // 不传 = 全部 type
  entityId?: string;        // 不传 = 全部 id (按 entityType 过滤)
  callback: (change: EntityChange) => void;
}): Subscription;
```

**典型用法（React 风格）**：

```typescript
function EntryCard({ entityId }) {
  const [entity, setEntity] = useState(initialSnapshot);

  useEffect(() => {
    const sub = subscribeEntityChanges({
      entityType: 'entry',
      entityId,
      callback: (change) => {
        if (change.change_kind === 'hard_removed') {
          // 父组件应该响应并 unmount; 此组件不主动处理
          return;
        }
        if (change.change_kind === 'soft_deleted') {
          setEntity({ ...change.snapshot });  // snapshot.deleted=true
          return;
        }
        // updated / restored / created
        setEntity({ ...change.snapshot });
      },
    });
    return () => sub.unsubscribe();
  }, [entityId]);

  if (entity.deleted) return <DeletedEntryStub />;
  return <div>{entity.data.title} - {entity.data.body}</div>;
}
```

### 4.7 防过度重渲染的几个反模式

- **AP-PROTO-1** 前端不按 entity_id 订阅，监听整 doc → 任何 op 都触发全 doc re-render
- **AP-PROTO-2** EntityChange 的 snapshot 没传 → 前端必须再查 doc.get(...) → 增加 round-trip
- **AP-PROTO-3** patches 直接 broadcast 给前端 → 前端要自己聚合，重复计算
- **AP-PROTO-4** 渲染单元 key 用 entity.name 而不是 entity_id → name 改了组件不连续，前端 unmount + mount，状态丢
- **AP-PROTO-5** 一个组件订阅多个 entity_id → 一个 entity 变了整组件重渲染（应该拆 sub-component 各自订阅）

## 5. 异常分支总览

| 错误码 | 来源叶子 | 何时抛 | caller 该做 |
|---|---|---|---|
| `unknown_command` | command | dispatch 一个未注册 id | 提示业务方拼错 |
| `acl_denied` | command / lock | source 不在 sources_allowed / 别人持锁 | 提示用户 / 提供 force 选项 |
| `resource_unavailable` | resource-budget | 闸门拿不到资源且 on_violation≠wait | 按 on_violation：reject 拒绝 / cancel_retry 由 caller retry |
| `resource_budget_exceeded` | resource-budget | 运行时自证：活跃计数 > 预算（闸门被错配）| 别 catch；冒出修架构（AP-RB-7）|
| `concurrency_audit_failed` | resource-budget | 发版前乘法审计：路径并发乘积 > 预算 | 改声明 / 降并发 / 调预算，发版前解决 |
| `poll_failed` | command | spec.poll 返 false | 业务方查 args 是否满足前置条件 |
| `validation_failed` | 各处 | 各种参数校验 + execute 没返 Operation | 看 context 细节 |
| `cas_version_mismatch` | entity / resolver | expected_version != current_version / 3-way 不同值 | 查 base + retry / merge / 给用户选 |
| `read_only_violation` | entity | 写 read_only entity | 拒绝；read_only 来源数据不能改 |
| `entity_soft_deleted` | entity | 软删后再 update | 先 restore 再 update |
| `entity_not_found` | document | doc.get 找不到 | 已被别人 hard_remove |
| `entity_file_missing` | document | document.json 索引指向不存在的 entity 文件 | 数据损坏；从备份恢复 |
| `entity_file_identity_mismatch` | document storage | entity payload 的 exact type/id 与 collections identity 不同 | fail closed；隔离项目并从备份/repair 恢复 |
| `document_storage_unsupported` | document storage | 平台/文件系统缺 POSIX dirfd、`O_NOFOLLOW` 或 directory fsync 能力 | 不降级到便利 Path API；迁移到受支持存储 |
| `document_storage_path_unsafe` | document storage | owner/权限不安全，或遇到 symlink、hardlink、特殊文件、unsafe directory | 不跟随、不清理目标；隔离并人工检查项目树 |
| `document_storage_layout_invalid` | document storage | marker、generation/shard/file 名或 legacy 安全单组件布局不成立 | fail closed；走显式 repair/import，不猜路径 |
| `document_storage_mapping_collision` | document storage | 两个业务 identity 映射到同一 digest/inode/casefold 实体 | 不覆盖任一实体；停止保存并修复/升级映射 |
| `document_lock_unavailable` | document storage | 稳定锁文件不安全、平台无 flock、锁模式错误或有界等待超时 | 本次未越过 commit point；返回 busy/重试，不降级为无锁读写 |
| `project_recovery_required` | document storage | durable JSON、元数据、时间、资源边界或读取一致性损坏 | 项目进入 recovery；不能当用户输入错误继续服务 |
| `document_write_failed` | document storage | commit point 前的 file write/fsync/rename 被明确拒绝 | 由 CommitReceipt 决定 rollback/retry；调用方不得仅凭异常猜 durable |
| `document_durability_unknown` | document storage | `document.json` rename 已可见，但 root directory fsync 无法确认 | 禁止 rollback、OpLog/publish 与自动重放；按 visible version 对账修复 |
| `schema_violation` | entity (validator) | data 字段不符 schema | 业务方检查 schema 跟 patches 是否对应 |
| `version_conflict` | document | expected_version != 内存 `_v`，或锁内 durable `_v` 已前进 | 本次零权威写入；回滚当前 mutation，reload 后按新 head 重试/合并 |
| `asset_not_found` | asset | store.get(ref) 但 blob 不存在 | 数据缺失；重新上传 |
| `asset_corrupted` | asset | checksum 不匹配 | 磁盘损坏；从备份恢复 |
| `cancelled` | pipeline | stage 内 checkpoint() 命中 cancel | 用户主动取消，正常分支 |
| `stage_error` | pipeline | stage.run 抛 generic Exception | 看 trace_id 查日志 |

## 6. undo / redo 流程（设计已就位，实施 ⏳ 等 04-history-rollback）

```
[用户点 undo]
   ↓
取 OpLog 最后一条 op (不是 kind=rollback 的, 不然就是 redo)
   ↓
反向 apply op.reverse_patches:
  for patch in op.reverse_patches:
      doc.apply_patch_directly(patch)   # ⏳ 这个 API 等 04 启动后加
   ↓
产生 rollback Operation:
  rollback_op = new_operation(
      command_id="__undo__",
      source="user",
      actor=undo_actor.to_dict(),
      patches=op.reverse_patches,        # 反向 patches 作为正向
      reverse_patches=op.patches,         # 原 patches 作为 reverse (支撑 redo)
      group_id=op.group_id,               # 同 group_id 一起 undo
      kind="rollback",
      trace_id=op.trace_id,                # 串起来
  )
   ↓
op_log.append(rollback_op)               # rollback 也是 op
   ↓
按 §4 流程产生 EntityChange + broadcast + 前端重渲染
```

**关键**：rollback 也是 op，落同一个 OpLog；前端按一样的 EntityChange 协议重渲染——rollback 看起来跟普通变更没区别（这正是协议优雅之处）。

[redo] 就是 undo 一个 kind=rollback 的 op。

## 7. 跟其他叶子 SPEC 的关系

| 叶子 SPEC | 跟本文档关系 |
|---|---|
| 各叶子 SPEC §1-§10 | 局部规则；本文档**串联** |
| 各叶子 SPEC §9 (audit gates) | 本文档 §2 / §5 引用 gate 编号；[AUDIT-GATES.md](./AUDIT-GATES.md) 集中维护 |
| 各叶子 SPEC §11 (实装状态) | 本文档 §2 时序图标 ⏳ 处对应未实装段 |

业务方读叶子 SPEC 理解局部；读本文档理解全链路。审计 gates 列在 [AUDIT-GATES.md](./AUDIT-GATES.md)。

## 8. 当前实装状态

### ✅ 已实装（后端完整链路通了）

- §2 第 1-10 步 — 全部叶子已实装（document / entity / asset / operation / command / pipeline / concurrency / conflict-resolution）
- §2 第 11 步 — `_protocol/change_set.py` 提供 `affected_entities()` 聚合工具
- §4 EntityChange 数据结构 + broadcast payload 格式定义

### ⏳ 待补（真有跨叶子依赖）

- §2 第 12 步 broadcast 实施 — 等 [01-realtime-sync/](../02-data-flow/01-realtime-sync/)
- §2 第 13-14 步 前端订阅 + 渲染 — 等 [04-user-facing/](../04-user-facing/)
- §6 undo / redo 控制器实施 — 等 [04-history-rollback/](../02-data-flow/04-history-rollback/)
- 离线场景 + CRDT — 等 [03-offline-first/](../02-data-flow/03-offline-first/)

### 原则

- **本文档是契约不是实施**：签名 / 数据结构 / 时序固定；实施分散到各叶子
- **可机器校验**：每条规则配 audit gate（见 [AUDIT-GATES.md](./AUDIT-GATES.md)），脚本可跑
- **前端协议先定**：实施等 01-realtime-sync / 04-user-facing 启动；签名固定不再变
