# 04-history-rollback — History & Rollback

> 一句话：**撤销 / 重做 / 命名快照 / session group 回滚**。`UndoController` 跑通用户撤销链路；bootstrap-bound `HistoryAuthority` 保证受管项目只从同一条 fresh canonical history 推导、再由同一 coordinator/store 写回；`CheckpointManager` 已提供整文档快照。

## 在框架里的位置

- 上一级：[02-data-flow/](../) — 数据流转 5 个叶子
- 同级：[01-realtime-sync/](../01-realtime-sync/) / [02-conflict-resolution/](../02-conflict-resolution/) / [03-offline-first/](../03-offline-first/) / [05-concurrency/](../05-concurrency/)
- 改动：[01-document/](../../01-data-structure/01-document/) 加 `Document.apply_patch_directly`（框架内部 hook）

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览）|
| [SPEC.md](./SPEC.md) | 11 节完整规则（UndoController / apply_patch_directly / group_id / Checkpoint）|
| [undo.py](./undo.py) | UndoController + rollback_field + rollback_entity_to_op |
| [checkpoint.py](./checkpoint.py) | CheckpointMeta + CheckpointManager 整文档快照 |
| [test_undo.py](./test_undo.py) | pytest（含 apply_patch_directly / checkpoint / session rollback 集成）|

## 当前状态

**✅ 已实装**（无跨叶子依赖项全做）

- `UndoController(op_log, doc)` — 双栈撤销/重做
  - `prepare_undo(actor)` — 在 coordinator transaction 的单一 fresh OpLog snapshot 中取末尾未撤销 native group；只 apply + 产待提交记录
  - `prepare_redo(actor)` — 在同一 snapshot 中选择 redo_stack 顶部对应的 committed undo；只 apply + 产待提交记录，不提前 pop
  - `confirm_deferred(record)` — exact durable suffix 与 source ticket 已确认后才 push / pop redo 栈
  - `discard_deferred(record)` — 仅在 coordinator 证明 storage/head 未变化且 Document 已由 W04 恢复后释放未决凭据；unknown outcome 不可 clean discard
  - `undo / redo(actor, persist=...)` — 仅未绑定 canonical OpLog 的 legacy caller 兼容入口，仍执行 apply → persist → append → confirm；bootstrap 后的项目即使没有 mutation policy 也不允许走这条直连路径
  - `can_undo() / can_redo()` — 状态查询
- `rollback_field(entity, field, target, actor, group_id="")` — 工厂：造 `kind=rollback` Operation（含 set patch）
- `rollback_entity_to_op(entity, op_log, target_op_id, actor)` — 工厂：累积反向 patches 让 entity 回到某 op 后状态
- `rollback_operation_group(op_log, doc, actor, group_id|session_id)` — 只给未绑定且无 policy 的 legacy runtime 使用；它按 group/session 回滚并自行 append。bootstrap-bound OpLog 或带 policy Document 都在读 source 前 fail closed，不能借 history 特权绕 W03；受管生产入口只走 `prepare_undo/redo → CommitCoordinator`
- `Document.apply_patch_directly(...)` / `apply_patches_atomically(...)` — 仅供 history 模块持有私有 capability 的重放路径；支持 set/delete/create/soft_delete/restore/name，走 CAS、完整值域预算、policy coverage 与最终 cardinality，不走 command ACL。对 bound runtime，capability、active exact coordinator transaction 与 `HistorySourceTicket` 必须同时成立；带 policy 的 Document 另要求 coordinator coverage，普通调用在任何变更前拒绝
- `HistoryAuthority`（位于 [`_protocol/history_authority.py`](../../_protocol/history_authority.py)）— bootstrap 用一次性 bind capability 把一个 project generation 的 exact Document / OpLog / UndoController / CommitCoordinator / CommitStore（当前为 FileCommitStore）与 runtime revision 绑定；只提供只读诊断 handle，写能力保留在 coordinator 私有 port
- `HistoryAuthorityError` — authority protocol 违约用 exact type 报告，固定 code、稳定 reason、typed `outcome_unknown` / `durability_phase`，以及仅在 commit 边界复制进去的 JSON-like `commit_facts`
- policy seal 边界 — unsealed 只属于未发布 bootstrap/migration 准备态；完整 command registry 校验后单向 seal。受保护 replay 即使持有 history capability，也必须由 CommitCoordinator 激活 coverage；capability 不能单独扩权
- `CheckpointMeta` dataclass — 记录 checkpoint id / document id / reason / session / summary
- `CheckpointManager.create / restore / list / fork` — 当前为整文档快照实现,后续可替换为 AssetStore 增量
- server 的 undo/redo 生产入口已用 CommitCoordinator 提交 rollback Operation，canonical event 与普通命令走同一发布边界

**⏳ 待补**（真有跨叶子依赖）

- `CheckpointManager` 增量化 — 等 [03-asset/](../../01-data-structure/03-asset/) 内容寻址做 blob 复用
- 跨 Doc undo / session rollback UI — 业务方需求驱动

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态).

## 关键设计决策

- **走 entity CAS，不绕审计** — undo 调 `entity.update / delete / restore`，actor_log 完整记录"undo 也是变更"；前端 broadcast 能看出"entity 又变了"
- **跳过 command ACL，但不开放旁路** — undo 是系统级动作，不应被 `command_dispatch.sources_allowed` 拦；只有历史模块持有的私有 capability 能进入 direct replay，业务代码直接调用会失败
- **group_id 聚合**："连续 + 同 group_id"才一起 undo；中间换 group_id 或遇 rollback 即止 — 避免"很久以前同 group_id 的 op 被一起 undo"
- **redo_op 也是 `kind="rollback"`** — redo 在数据层就是再做一次 undo；统一 kind 简化"非 rollback 才是用户原操作"过滤逻辑
- **forward_patches 旧→新顺序** — undo 后保存进 rollback_op.reverse_patches 时按时间顺序排列，让 redo 能正确依次 apply（`set` patch 用 `after` 字段，顺序错会被后做的覆盖）
- **reverse 是倒序程序** — 先按 operation 新→旧，再按各 Operation 已记录的 reverse_patches 顺序执行；missing 字段靠 `delete` 而非 null，dot-path 父链/provenance 交换端点，结构 create/delete 保留 exact position
- **历史游标晚于耐久提交** — deferred producer 不保存 Document、不 append OpLog，也不动 redo 栈；提交器先原样提交 `record.operations`，再 `confirm_deferred`。未落日志确认、跨 controller 确认、重复确认和并发乱序都会被拒绝
- **source 只从一个 fresh head 推导** — 每次 `CommitCoordinator.execute` 打开一个 history transaction 并固定 trusted OpLog head；第一次读 history 得到一个不可变 rows snapshot，undo/redo 的全部分组、stale-redo 判断与 source 选择都使用它，不允许一次动作混读两个 head
- **runtime graph 发布是一笔原子安装** — registry exact-object/path 边、UndoController epoch/PID 与 Document 的 OpLog/controller/epoch/PID 在同一 registry lock 下发布；叶子 hook 抛普通异常或 process-control 都撤销本次全部边并按 exact epoch/PID 回滚已安装部分，一次性 bind capability 仍可安全重试。安装成功的同一 Document 永不原位换代重绑
- **deny-only fresh verifier 不制造反向锁序** — delegated authority check 先在 registry 下核对 exact ACTIVE graph，释放 registry lock 后读取 OpLog，再回 registry 双检 graph/trusted head；合法 coordinator 推进可有界重试，稳定 head 的读取失败会 freeze 为 typed `history_runtime_snapshot_unavailable`。它不返回 handle、transaction、ticket、permit 或任何写权
- **source ticket 与 append permit 都是一次性的** — source indexes 在同一 transaction 内只能 claim 一次，ticket 绑定 source canonical body、controller、purpose 与 exact replay Operation；store 前还要证明该 Operation 在 commit intent 中恰好出现一次。OpLog 每次 append 另领一个 exact-object/body/index permit，成功发布即消费，失败只在 fresh head 证明未变化时可 clean discard
- **耐久确认不是“append 没抛错”** — 所有 intent append 完后，authority 从原 source head 读取 durable suffix，逐条核对数量和 canonical Operation body；不完整、正文不符或无法确认一律 freeze，而不是推进 redo 栈或自动重放
- **freeze / retire / reload / fork 各自唯一职责** — head 漂移、post-write 验证失败或未知结果会 freeze 当前 authority；freeze 不等于 unbind，后续请求必须 reload/repair。只有 bootstrap 私有 capability 能在 supervisor 安全停住且无 active transaction 后 retire exact graph。fork child 会清空继承 capability，并 poison 继承 OpLog 对象/路径，必须用 fresh OpLog 与 fresh bootstrap bind 重建
- **FileCommitStore 边界保持诚实** — 当前 store 先发布完整 Document generation，再逐条 append OpLog；Document head 与 OpLog 是两个独立耐久域，没有共同物理 commit point。HistoryAuthority 证明的是“谁可读写、source 是否 fresh、写入 suffix 是否 exact”，不能抹掉中间崩溃窗口；`outcome_unknown=True` 时必须 repair/reconcile，不能 reload 后盲重试

## 怎么用

### 撤销 / 重做（CommitCoordinator 推荐路径）

```python
from entity import Actor
from sdk import MutationBatch

# 正常生产 caller 消费 bootstrap 创建并绑定好的 Context；不要自行 bind HistoryAuthority。
controller = ctx.undo
commit_coordinator = ctx.commit

# 用户做过的操作已经走 CommitCoordinator；成功 receipt 证明 Document + OpLog 都耐久。
# 当前 FileCommitStore 的 Document generation 与 OpLog 仍不共享物理 commit point。
# ...

# 用户按 Cmd+Z；prepare 必须在 coordinator producer 内执行，才能与外层
# mutation session、store 与 canonical event 属于同一写锁/提交尝试。
prepared = {}

def produce_undo():
    record = controller.prepare_undo(actor=Actor(user_id="u1"))
    prepared["record"] = record
    if record is None:
        return None
    return (
        record.operations[0]
        if len(record.operations) == 1
        else MutationBatch(record.operations)
    )

execution = commit_coordinator.execute(produce_undo)
record = prepared.get("record")
if record is not None:
    if execution.is_committed:
        controller.confirm_deferred(record)
    else:
        # CommitCoordinator 已按 receipt 处理 Document rollback/repair；这里只释放游标凭据。
        controller.discard_deferred(record)

# Cmd+Shift+Z 同理；prepare_redo 不会提前消费 redo 栈。
```

`undo(actor, persist=...)` / `redo(actor, persist=...)` 只为旧接线保留；新写入口不要再手工拼
save + append + publish。这里的“旧接线”只指没有 bootstrap canonical binding、也没有 mutation
policy 的独立 legacy 对象；正常 `Context` 一经 bootstrap 就必须走 exact coordinator，即使该 Document
暂未声明 policy 也不能降级成直连 helper。

### 私有 replay seam 与 rollback 工厂

`apply_patch_directly` / `apply_patches_atomically` 不是 mod API，也不提供可复制的公开调用示例：它们要求 history 模块持有的 exact capability。普通 caller 即使构造形状相同的 token，也会在任何数据变化前拒绝。bootstrap-bound runtime 还要求 active exact CommitCoordinator transaction 签发的 source ticket；带 policy 的 Document 另要求 coordinator 激活 coverage，最终由同一 coordinator/store 提交同一 rollback Operation。

这是一条防框架同进程误接线的内部能力边界，不是 hostile Python sandbox：能够任意导入私有符号或篡改进程内存的代码不在本合同的攻击模型里。

`rollback_field` / `rollback_entity_to_op` 是构造 rollback Operation 的低层工厂，不拥有 apply、save、append 或 publish 权。新生产入口优先使用 `UndoController.prepare_undo/prepare_redo`；只有 history 内部适配器才能把工厂产物交给私有 replay seam，并仍需走统一 coordinator。

## 跟其他叶子的关系

| 叶子 | 关系 |
|---|---|
| [01-document/](../../01-data-structure/01-document/) | **本叶子加 `apply_patch_directly`** 进 Document（框架内部 hook）|
| [02-entity/](../../01-data-structure/02-entity/) | 调 `entity.update / delete / restore` 反向 apply |
| [02-operation/](../../03-commands-operations/02-operation/) | 读 OpLog；造 `kind="rollback"` Operation；耐久 append 归 CommitCoordinator |
| [01-command/](../../03-commands-operations/01-command/) | **不**依赖 CommandRegistry；undo 走 apply_patch_directly 绕 ACL |
| [_protocol/](../../_protocol/) | `history_authority.py` 持有 canonical runtime binding 与 typed failure；另有 5 个 GATE-UNDO-* gate |
| [03-asset/](../../01-data-structure/03-asset/) | Checkpoint 真实施时复用 |

## 跟 audit gate 关系

| Gate | 验证 |
|---|---|
| `GATE-UNDO-01` | undo 后 OpLog 末尾 `kind=rollback` |
| `GATE-UNDO-02` | undo 后 redo 恢复原数据状态 |
| `GATE-UNDO-03` | 私有 replay seam 处理 set patch + CAS bump version |
| `GATE-UNDO-04` | 元操作 create / soft_delete / restore / hard_remove 全跑通 |
| `GATE-UNDO-05` | 同 group_id 多 op 一次 undo 反所有 |
注册在 [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md)。

## 跑测试

```bash
cd 01-core/02-data-flow/04-history-rollback/
python -m pytest test_undo.py -v

# 跑 audit gates
python ../../_protocol/audit_gates.py --leaf UNDO
```

## 下游 mod 测试惯用法(coordinator 时代)

OpLog 协调器独占写权后,mod 测试**不能**裸 `dispatch(..., op_log=ctx.op_log)`(不入账)、
不能手动 `op_log.append`(被历史权威拒)、不能裸用 `UndoController`(被 bind 校验拒)。
正门编舞(design coresync 首航验证的最小 helper,建议 mod 测试直接抄):

```python
def _commit_run(ctx, cmd, args, actor):
    """经协调器提交一笔命令 — 唯一合法写路径。"""
    return ctx.commit.execute(
        lambda: ctx.cmd_reg.dispatch(cmd, args, actor=actor, source="ai",
                                     doc=ctx.doc, op_log=None))

def _commit_undo(ctx, actor):
    """经协调器撤销:prepare → execute 提交 → confirm。"""
    record = ctx.undo.prepare_undo(actor)
    ctx.commit.execute(lambda: record.operations)
    ctx.undo.confirm_deferred(record)
```

要点:①单变更会话下先 `with ctx.lock`(否则 mutation_session_busy);②反向补丁断言用属性
(`p.field` / `p.after`),不是 dict 下标;③requires_confirm 三段化 = `reserve_confirmation →
confirm(reservation) → pending_store.consume/finalize`,逐段驱动,不要指望裸 dispatch 落库。
