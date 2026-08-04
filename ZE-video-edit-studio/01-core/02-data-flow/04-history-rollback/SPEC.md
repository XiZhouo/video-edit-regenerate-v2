# History & Rollback — 撤销/重做 + 检查点

## 1. 这是什么

`UndoController`：跑通"用户撤销/重做"全链路。`HistoryAuthority`：由 bootstrap 把一个 project generation 的 exact Document、OpLog、UndoController、CommitCoordinator 与 CommitStore 绑定为唯一 canonical history runtime，并把 source freshness、replay derivation、write intent 与 durable suffix 连成一条可验证链。`CheckpointManager`：命名快照,当前用整文档 JSON 快照实现,后续可替换为 [03-asset](../../01-data-structure/03-asset/) 内容寻址增量。`rollback_operation_group`：只为未绑定 legacy 对象保留的 session/group helper。

业务白话视角：业务同事在 ZE-seek-find-object-generator 上拖错一个物体的尺寸 — Cmd+Z 应该精确撤回那一次拖动；同次拖动产生的多次 micro-op 合一次撤。生产流程是：用户按快捷键 → CommitCoordinator 的 producer 调 `prepare_undo` → 私下原子 apply OpLog 目标的 `reverse_patches` 并产一条 `kind="rollback"` Operation → CommitCoordinator 统一提交 Document + OpLog + canonical event → 成功后才 `confirm_deferred` 推进本地 redo 游标。

### 为什么是独立叶子

| 问题 | 原因 |
|---|---|
| **OpLog 只是日志，不会自己反推** | OpLog 有 `reverse_patches` 但没有人去 apply；UndoController 负责调度 |
| **CAS 协议不能绕** | undo 不能"直接改 entity 内部状态"，得走 `entity.update(..., expected_version=...)` 保持 actor_log + version 完整；但 command ACL / pipeline gating 又得跳过（否则 undo 撞 sources_allowed） |
| **group_id 是聚合关键** | 拖动产生 10 个连续 op 同 group_id → 一次 undo 应当反 10 条；不靠 group_id 用户体验崩 |
| **redo 用 rollback 的 reverse** | rollback op 自己也有 reverse_patches（"再 apply 一次"），redo = undo 那条 rollback op |
| **同一路径不能有两个 history authority** | 仅凭“对象指向同一路径”不足以证明它是 canonical writer；必须绑定 exact runtime graph，并拒绝 alias、过期 generation、已 freeze/retire 与 fork 继承对象 |
| **读到旧 source 比 patch 算错更危险** | undo/redo 必须从 coordinator transaction 固定的同一个 fresh head/rows snapshot 推导，不能先读一次 OpLog、apply 后再从另一个 head 确认 |
| **写入成功不等于完整提交可证明** | FileCommitStore 的 Document generation 与 OpLog 是两个独立耐久域；必须再核对从 source head 开始的 exact durable suffix，不完整或不确定就 freeze、repair，绝不自动重放 |
| **Checkpoint 是另一个维度** | 命名快照不在主链路；当前先整文档快照,后续可增量化 |

## 2. 数据模型

### UndoController

```python
class UndoController:
    """双栈撤销/重做控制器.

    持有 OpLog (读历史 op) + Document (反向 apply patches).
    redo_stack 存 undo 过的 rollback_op 的 id; redo 时再做一次反向 apply.
    """

    def __init__(self, op_log: OpLog, doc: Document) -> None: ...

    def prepare_undo(self, *, actor: Actor) -> DeferredHistoryRecord | None: ...
    def prepare_redo(self, *, actor: Actor) -> DeferredHistoryRecord | None: ...
    def confirm_deferred(self, record: DeferredHistoryRecord) -> None: ...
    def discard_deferred(self, record: DeferredHistoryRecord) -> None: ...

    # 仅未绑定且无-policy的 legacy caller兼容；bootstrap runtime 必须经 exact coordinator
    def undo(self, *, actor: Actor) -> list[Operation] | None: ...
    def redo(self, *, actor: Actor) -> list[Operation] | None: ...
    def can_undo(self) -> bool: ...
    def can_redo(self) -> bool: ...
```

`DeferredHistoryRecord` 是短生命周期提交凭据，不是新的持久化历史模型。对上层只暴露：

- `kind`：本次是 undo 还是 redo；
- `operations`：CommitCoordinator 必须作为一次逻辑提交原样落下的 Operation tuple；
- `token`：确认或丢弃时用于配对。

记录内部还钉住 controller 身份、prepare 前 OpLog 长度、redo 栈快照和 redo 目标。这样
`confirm_deferred` 能拒绝跨实例、重复确认、未落日志确认和并发乱序确认，而不是“相信 caller
说已经成功”。同一 controller 同时只允许一个未决 record，防止第二次 prepare 基于旧日志重复
apply 同一 group。

### Canonical HistoryAuthority（框架私有）

实现位于 [`_protocol/history_authority.py`](../../_protocol/history_authority.py)。它不是新的持久化
日志，也不是 mod API；OpLog 仍是 canonical durable history。`HistoryAuthority` 只是一个只读诊断
handle，能报告 state、trusted head、project id、generation、runtime revision 和 freeze reason，不能自行
打开 transaction、选择 source 或写 OpLog。

bootstrap 持有一次性 bind capability，并在 Context 发布、`PipelineSupervisor.recover()` 开始之前原子绑定：

- exact `Document`、`OpLog`、`UndoController`、`CommitCoordinator` 与 `CommitStore` 对象；
- `project_id`、拓扑 `generation` 与当前 core `runtime_revision`；
- 绑定时 fresh OpLog head：`count + tail_hash + canonical path identity + stat signature`；
- bootstrap 固定的纯 replay derivation verifier。

绑定同时按 coordinator、controller、OpLog exact identity 和 canonical path 建唯一索引。丢弃公开 handle
不会解除保护；authority 有独立强生命周期根，只有 bootstrap 私有 retire capability 能释放。第二个对象
即使打开同一路径也只是 alias，不能借合法 transaction 的 write scope 写入。

这些 registry 边与 runtime 叶子不是两次独立发布。bind 在同一 registry lock 内先建立临时 exact graph，
再把同一 authority epoch/PID 一次性安装进 UndoController 和 Document；任一 hook 抛普通异常或
`BaseException`，都会撤销本次 coordinator/controller/OpLog/path/port/handle 边，并用精确 epoch/PID
回滚已安装叶子。失败不消费 bind capability，修复运行态后可重试；成功安装过的同一 Document 则永久
拒绝第二代 authority，即使上一代已 retire，reload 只能创建 fresh Document/Context。

Batch 4 的 private deny-only runtime verifier 还会核对 store→OpLog、controller→Document/OpLog 以及
Document/controller 的 epoch/PID 都与中立 record 完全一致。它不得持 `_REGISTRY_LOCK` 再取 OpLog lock：
固定流程是 registry 预检 → 无 registry 锁 fresh snapshot → registry 后检；期间若合法 coordinator 推进
trusted head，最多有界重试，稳定 head 的 snapshot I/O/解析失败则 freeze 并抛 typed
`history_runtime_snapshot_unavailable`。返回值只有 detached rows 与 fresh head，不暴露 authority handle、
transaction、source ticket、append permit 或 replay capability。

```python
class HistoryAuthorityError(RuntimeError):
    code: Literal["history_authority_mismatch"]
    reason: str
    outcome_unknown: bool
    durability_phase: Literal[
        "preflight", "commit_outcome", "post_write_verification"
    ]
    commit_facts: dict[str, JSONValue]
```

`reason` 用于诊断具体违约；**能否重试只看 typed `outcome_unknown`，不从 reason/message/code 猜**。
普通 identity/head/ticket 违约是 `preflight + outcome_unknown=False`，要求 reload；coordinator 已得到不可
发布 receipt 且不能证明完整 history suffix 时是 `commit_outcome + outcome_unknown=True`；append/suffix
发布后的不完整、正文不符或无法 fresh 验证是 `post_write_verification + outcome_unknown=True`。后两类
必须 repair/reconcile，禁止自动重放。

neutral protocol 不 import `CommitReceipt`。只有 CommitCoordinator 在 commit-outcome 边界把 receipt 的
`phase/outcome/commit_id/retry_safe/document_durable/oplog_durable` 与可选 failure facts 复制为 JSON-like
`commit_facts`；复制后与原对象脱离，Pipeline 等消费者不能持有或篡改原 receipt。

### rollback 工厂

```python
def rollback_field(
    entity: Entity, field: str, target_value: Any,
    *, actor: Actor, group_id: str = "",
) -> Operation:
    """造一个 rollback Operation: 把 entity.data[field] 设回 target_value.
    实际 apply 留给 caller (e.g. UndoController.undo).
    """

def rollback_entity_to_op(
    entity: Entity, op_log: OpLog, target_op_id: str,
    *, actor: Actor,
) -> Operation:
    """造 rollback Operation: 把 entity 状态回到 target_op_id 之后的状态.
    实施: 找 op_log 中 target_op_id 之后影响该 entity 的所有 op,
         它们的 reverse_patches 拼起来.
    """
```

### CheckpointManager

```python
@dataclass
class CheckpointMeta:
    name: str
    checkpoint_id: str
    document_id: str
    document_version: int
    created_at: float
    actor: dict
    op_log_position: int       # OpLog 中的 position (后续支持回到)
    summary: dict               # {entity_count, asset_count, ...}
    reason: str                 # manual / session_open / before_task / ...
    session_id: str
    group_id: str

class CheckpointManager:
    """命名快照管理器.

    当前写 `<doc_dir>/checkpoints/<name>.json`,包含 meta + document snapshot。
    """

    def create(self, doc: Document, name: str, *, actor: Actor) -> CheckpointMeta:
        ...

    def restore(self, name: str) -> Document:
        ...

    def list(self) -> list[CheckpointMeta]:
        ...

    def fork(self, name: str, new_name: str) -> CheckpointMeta:
        ...
```

## 3. undo / redo 流程

```
用户按 Cmd+Z（新统一提交路径）
  ↓
CommitCoordinator.execute 在 generation lease + 项目写锁内打开 exact history transaction
  ↓
核对 coordinator/store/document/op_log identity，fresh head 必须等于 authority.trusted_head
  ↓
producer 调 UndoController.prepare_undo(actor=...)
  ↓
replay port 第一次读取并固定一个 fresh rows snapshot；本动作后续不再另读 OpLog
  ↓
从该 snapshot 选完整连续 native group，并签发一次性 HistorySourceTicket
  ↓
造 rollback_op = new_operation(
    kind="rollback",
    command_id="undo",
    source="user",
    actor=actor,
    patches=<拼接的反向 patches>,      # undo 实际应用的
    reverse_patches=<原 op 的正向 patches>,  # redo 时用
    group_id=<原 group_id 或新生成>,
    trace_id=<原 trace_id>,
)
  ↓
bootstrap-sealed verifier 证明 exact source body → rollback_op derivation，并把 ticket 绑定该实例/正文
  ↓
history capability + active exact transaction + source ticket（受保护类型另需 policy coverage）调 apply_patches_atomically(...)
  ↓
返回 DeferredHistoryRecord(operations=(rollback_op,))
  ↓
coordinator.prepare_store 证明 ticket 已 replay，且同一 rollback_op 在 commit intent 恰好出现一次
  ↓
FileCommitStore 先提交 Document，再在私有 write scope 中逐 Operation 领取/消费 append permit
  ↓
若 receipt 可发布，authority 从旧 head 读取 durable suffix，逐条核对数量与 canonical body
  ↓
source ticket 进入 DURABLE；authority.trusted_head 只在这一步推进
  ↓
controller.confirm_deferred(record)
  ↓
再次核验 controller 自己的 body ledger/redo snapshot，并消费同一 DURABLE source ticket
  ↓
redo_stack.append(rollback_op.id)
```

prepare 阶段明确禁止 save / append / publish / push redo 栈。提交失败时，W04 的外层
CommitCoordinator session 只有在 receipt 能证明 storage untouched、且 fresh head 仍完全等于 source head
时，才恢复 Document 并允许 `discard_deferred(record)` 消费 DISCARDED ticket；discard 自己不 blind-set
数据。Document/OpLog 可能已写入或 durable suffix 无法证明时，authority 进入 FROZEN 并抛 typed
`HistoryAuthorityError(outcome_unknown=True)`，ticket 不能被伪装成 clean discard，也不能自动重放。

旧 `undo(actor, persist=...)` 只为**未绑定 canonical OpLog 且无 mutation policy**的 legacy caller 保留，
内部按 apply → persist → append → confirm 执行。正常 bootstrap Context 即使当前没有 policy，也已经
绑定 exact OpLog/coordinator，直连 helper 会在读取 source 前 fail closed，不能绕过 W03。

这里的 protected Document 已经过完整 command registry validation 并单向 seal；unsealed 只允许未发布 bootstrap/migration 准备，不是 history 或 public request 的兼容开关。对 bootstrap-bound runtime，history 私有 capability、active exact coordinator transaction 与该 transaction 签发的 source ticket 必须同时成立；带 policy 的 replay 还必须有 active coverage。任一 capability 都不能单独扩权。它约束正常框架路径中的同进程误用，不宣称抵御能够任意 import 私有符号或改写内存的 hostile Python。

```
用户按 Cmd+Shift+Z (redo)
  ↓
caller 调 UndoController.prepare_redo(actor=...)
  ↓
从同一 history transaction 的 fresh rows snapshot peek redo_stack 顶 rollback_op_id（此时不 pop）
  ↓
只允许选择一条已 committed 的 `command_id=undo, kind=rollback`，签发一次性 source ticket
  ↓
对 reverse_patches 顺序 apply (= 重做原变更)
  ↓
造 redo_op 并证明 source→replay derivation，ticket 绑定 exact Operation/body
  ↓
返回 DeferredHistoryRecord(operations=(redo_op,))
  ↓
同一 coordinator/store 逐 append 并通过 exact durable suffix 验证
  ↓
controller.confirm_deferred(record) 才 pop 对应 rollback_op_id
```

同一 authority transaction 也包住普通 command/pipeline commit：这类写入没有 history source ticket，
但仍必须把 exact operations 固定为 commit intent，逐 append 消费 permit，并在 receipt 可发布后验证完整
durable suffix。因此“一切 canonical OpLog append 都来自 exact coordinator/store”不是 undo 专属规则。

**为什么 redo_op 也是 kind="rollback"**：redo 在数据层就是再做一次 undo（撤销 undo）。统一 kind 简化过滤逻辑（"非 rollback 的 op" 才是用户原操作）。

## 4. doc.apply_patch_directly — 带私有 capability 的框架重放 seam

这是 [01-document](../../01-data-structure/01-document/) 已提供、只交给 history 模块消费的私有 replay seam，不是 mod/public 扩展点。

### 4.1 签名

```python
class Document:
    def apply_patch_directly(
        self, patch: Patch, *, actor: Actor, _replay_capability: object
    ) -> None:
        """框架内部用 (undo / sync 等): 把 patch 应用到 entity / doc.

        - 不走 command ACL (避免 undo 撞 sources_allowed)
        - 走 entity / doc 的 CAS (expected_version=当前 version), 落 actor_log
        - 不调 OpLog.append (由外层 CommitCoordinator 提交 rollback_op)

        支持 Patch.op 六态: set / delete / create / soft_delete / restore / name.
        """
```

这不是公开扩展点。`undo.py` 持有与 `document.py` 配对的模块私有 capability；普通业务、
插件或下游 Surface 无法构造等价 token。入口只接受 exact `Patch`。对 bootstrap-bound runtime，
`_history_operation_scope` 还必须验证 active coordinator transaction 的 `HistorySourceTicket` 精确绑定
当前 controller、Document、rollback Operation identity 与 canonical body，并在 replay 完成后把 ticket
推进为 REPLAYED；带 policy Document 另要求 coordinator 已激活 coverage。任一条件缺失都在 apply 前
fail closed。`create` 还要求 patch 目标的 type/id 与 payload 内 type/id 逐字一致，再走不重验当前
schema 的内部恢复插入；这样能忠实重放旧历史，但不能借历史接口伪造另一条 Entity。

### 4.2 6 种 op 的分发逻辑

| `patch.op` | `patch.field` | 行为 |
|---|---|---|
| `set` | 字段名（非 `__entity__`）| replay update 到 `after`；dot-path 若带 ancestry，再按 endpoint 恢复顶层父链；应用 provenance endpoint |
| `delete` | 字段名 | policy opt-in 真正从 data 移除该路径；无 policy legacy 文档保留旧 set-to-null 兼容。ancestry/provenance 仍按 endpoint 应用 |
| `create` | `__entity__` | 从完整 `after` envelope 恢复 Entity；受保护类型必须按 exact `position` 插回 canonical bucket |
| `soft_delete` | `__entity__` | `entity = doc.get(type, id); entity.delete(expected_version=..., actor=...)` |
| `restore` | `__entity__` | `entity.restore(expected_version=..., actor=...)` |
| `delete` | `__entity__` | 先核对完整 `before` envelope 与 exact `position`，再物理删；受保护类型缺 position 拒绝 |
| `name` | 任意字段名 | `entity.update({}, name=after, ...)` |

### 4.3 missing / null、倒序与可逆信封

history 按 `Patch.op` 判断 presence，而不能只看 before/after：从 missing 新建字段的 reverse 是 `delete`；显式 null 的 reverse 仍是 `set(..., after=None)`。多 patch 的 reverse 必须是**整个 forward program 的倒序**：撤一组 operation 时先 operation 新→旧，每个 Operation 内再按它已经记录好的 reverse_patches 顺序执行。

dot-path（如 `box.a`）授权时归顶层 `box`，但 replay 仍使用原始路径。父链原来 missing/null/scalar 时，Patch 必须带 `ancestry={field: root,before:{present,value},after:{present,value}}`；AI 来源使用同形状 `provenance`，其 field 为原始路径。reverse 两者都交换 before/after。结构 create/delete 的 `position` 在 forward/reverse 保持相同，保证 canonical 插入顺序恢复且 read-only overlay 始终位于其后。

`apply_patches_atomically` 按给定顺序应用整段；任一 patch、policy coverage 或最终 cardinality 失败时，恢复 Document、bucket、Entity identity、版本、actor log、provenance 与顺序到调用前。prepare 本身不 save/append/publish；rollback Operation 必须由同一 CommitCoordinator 提交。

**为什么走 entity CAS 而不是直接改 `_data`**：

- 保留 actor_log 完整性（undo 也是审计事件）
- 保持 version 单调递增（前端 broadcast 能看出"entity 又变了"）
- 跟 update 流程保持一致（出 bug 一处修）

**反过来，为什么跳过 ACL**：

- undo 是系统级动作，不应被 `command_dispatch.sources_allowed` 拦
- caller 已经决定要 undo；ACL 应在 caller / dispatch 层 gate，不在 patch apply 层

### 4.4 错误处理

- entity 不存在（set / delete / name op）→ 抛 `entity_not_found`
- entity 已存在（create op）→ 抛 `validation_failed`
- CAS 不匹配（version 不对）→ 抛 `cas_version_mismatch`
- 未知 op → 抛 `validation_failed`

UndoController 应在调 apply_patch_directly 前确保数据一致；CAS 失败属于罕见的并发场景（其他 actor 在 undo 之前改了同一 entity）。

## 5. group_id 聚合协议

**约定**：拖动 / 连续输入 / 批量编辑应在 command handler 用同一个 `group_id` 标记产生的多个 op。

UndoController.undo：

1. 取 OpLog 末尾第一个非-rollback 的 op，记 `target_group = op.group_id`
2. 如果 `target_group` 为空（""），单 op undo（不聚合）
3. 否则往前走，把所有 `group_id == target_group` 且非-rollback 的连续 op 收集
4. 反向 apply（从新到旧）

注意：**只往回收集"连续的"**——中间出现别的 group_id 或别人插入的 op 就停。这避免"很久以前同 group_id 的 op 被一起 undo"。

## 6. checkpoint 状态

| API | 状态 |
|---|---|
| `CheckpointManager.create` | ✅ 整文档快照 |
| `CheckpointManager.restore` | ✅ 返回新的 Document 实例,不自动覆盖当前文件 |
| `CheckpointManager.list` | ✅ 扫 `<doc_dir>/checkpoints/*.json` |
| `CheckpointManager.fork` | ✅ 复制 snapshot,生成新 checkpoint id |
| `CheckpointMeta` dataclass | ✅ 含 document/session/reason/summary |

**当前为什么不是增量**：增量快照真做需要：

- entity_state 序列化 → blob（hash 寻址）
- AssetStore 共享 blob 跨 checkpoint
- 反序列化 + 校验

当前整文档快照先把 `realtime_checkpoint` 的 session open / rollback / restore 跑通。后续切到 [03-asset/](../../01-data-structure/03-asset/) 增量 blob 时,上层 API 不变。

## 6.1 realtime_checkpoint group/session rollback

```python
rollback_operation_group(
    op_log,
    doc,
    actor=Actor(user_id="u1"),
    session_id="browser-session-1",
)
```

这是现存的 legacy/unbound-only compatibility helper：它仍自行 apply 并 append rollback Operation，不能作为新的 W03 生产接线。只有 OpLog 未被 bootstrap authority 绑定、Document 也无 mutation policy 的旧独立对象保持兼容；exact OpLog 已绑定或 Document 带 policy 时，都必须在读取 source、应用 patch 之前 fail closed。受管生产入口当前只能使用 `prepare_undo/prepare_redo → CommitCoordinator.execute → confirm_deferred/discard_deferred`。在完整 group rollback producer API 落地前，不得把这个 helper 包装成“已统一提交”。

规则：

- 必须传 `group_id` 或 `session_id`。
- 默认只回滚 `source="user"` 的 operation；AI task publish 不会被关闭网页的撤回顺手撤掉。
- 回滚前扫描被选中 operation 之后的同 entity/field 写入。
- 有后续同字段写入时默认 `conflict_policy="abort"`：返回 conflicts,不应用任何 patch。
- `conflict_policy="force"` 允许强制反向 apply,但调用方应在 UI/CLI 明确显示风险。
- legacy 无-policy 路径成功时追加 `command_id="rollback_operation_group"`、`kind="rollback"` 的 Operation；受保护路径不允许直连追加。

## 7. 跟其他叶子的关系

| 叶子 | 关系 |
|---|---|
| [01-document/](../../01-data-structure/01-document/) | **本叶子加 `apply_patch_directly`** 进 Document（框架内部 hook）|
| [02-entity/](../../01-data-structure/02-entity/) | 调 `entity.update / delete / restore` 反向 apply patches |
| [02-operation/](../../03-commands-operations/02-operation/) | 读 OpLog；造 `kind="rollback"` Operation；`new_operation` 工厂 |
| [01-command/](../../03-commands-operations/01-command/) | 不依赖 CommandRegistry；undo 直接经 apply_patch_directly 绕 ACL |
| [_protocol/](../../_protocol/) | `history_authority.py` 绑定 canonical runtime、签发一次性 ticket/permit、定义 typed failure；另有 5 个 GATE-UNDO-* gate |
| [07-persistence/](../07-persistence/) | CommitCoordinator 独占 authority transaction port；FileCommitStore 仍诚实报告 Document-generation/OpLog 两个耐久域的边界 |
| [03-asset/](../../01-data-structure/03-asset/) | Checkpoint 真实施时复用 |

## 8. 持久化协议

**redo_stack 不持久化**：跟 OpLog 不同，redo_stack 是 in-memory 的 UI 状态；进程退出丢失合理（重启后 OpLog 还在，"撤销"功能从 OpLog 末尾重新可用，但 redo 无意义）。

**rollback Operation 持久化进 OpLog**：跟普通 op 同等地位；OpLog `entries()` / `verify_chain()` 包含 rollback；`by_source(source="user")` 也能看到。

**Checkpoint 当前实现**：每个 checkpoint 把 meta + 完整 Document snapshot 写入 `<doc_dir>/checkpoints/<name>.json`，不污染 document.json；`restore` 返回新的 Document 实例，不自动覆盖当前 live 文件。未来增量化时才把重复 snapshot member 改为 AssetStore content-addressed blob，上层 API 保持不变。

**HistoryAuthority 完全 runtime-only**：exact object identity、trusted head、coordinator port、source ticket、
append permit、freeze state 与 runtime revision 都不写入 `document.json`、Entity、Operation、OpLog body、
Checkpoint 或任何 business snapshot。重启后由 bootstrap 从当前 durable Document/OpLog 和当前部署代码
重新创建 exact graph；不得从旧业务快照“恢复”进程内 capability。

**FileCommitStore 不是物理事务**：当前提交顺序固定为 Document save → 每条 OpLog append。Document 先把
完整 entity generation 耐久化，再用 `document.json` 原子替换发布唯一 head；OpLog 使用受锁 append + fsync。
这两个耐久域之间没有共同 commit point；Document 已耐久而 OpLog
只有前缀、未知或失败时，不能通过 HistoryAuthority 反向改写成原子成功/失败。authority 的职责是关闭
旁路、验证 exact suffix，并在不可证明时 freeze；真正跨文件/多进程原子性仍属于 W06 事务 store。

### 8.1 freeze、retire、reload 与 fork

- **freeze**：trusted head 漂移、runtime member 丢失、ticket/append 验证发生 post-write 不确定、receipt
  不能证明完整 suffix 时，authority 单向进入 FROZEN。freeze 保留 path ownership，不能退化成 legacy
  writer；同一旧 Context 的后续写入继续 fail closed。
- **retire**：只有 bootstrap 私有 capability 可执行；必须没有 active history transaction，并且上层先
  安全停住 Supervisor/worker。拓扑 delete/reset 成功或未发布 Context 构建失败时，Repository 才移除
  exact indexes/path lease；普通 handle holder 无权 unbind。
- **reload**：这里没有“把 FROZEN 改回 ACTIVE”的 API。reload 意味着先 repair/确认 durable head，安全
  retire 旧 Context，再由 fresh bootstrap 重新 open Document/OpLog 并 bind 新 authority。当前
  FileCommitStore 下，同 generation Document version 外部前进可能正处于“新 Document + 旧 OpLog”，所以
  Repository 明确返回 `commit_repair_required` 而不自动 reload；W06 单一事务 authority 落地前不能放宽。
- **fork**：child 进程只继承 Python 对象，不继承权威。`register_at_fork` 清空 capability/transaction/ticket
  registry，并 poison 继承 OpLog 对象及 canonical path；child 必须构造 fresh OpLog/runtime、取得 fresh
  bootstrap capability 再 bind，不能继续父进程 transaction 或把继承对象当 unbound writer。

### 8.2 typed failure 的统一消费

- HTTP server 只在信任边界把 exact `HistoryAuthorityError` 投影为公开
  `history_authority_mismatch`；`outcome_unknown=True` 的文案明确禁止重试并要求 repair/reconcile，
  preflight mismatch 才提示 reload。
- HTTP command/confirm/undo/redo、MCP tools 与 CLI command 都把 typed
  `history_outcome_unknown` classifier 交给 `IdempotencyStore.run`。新增的
  `outcome_unknown_classifier=None` 是尾部 keyword-only 可选参数；未传 caller 保持旧行为。
- classifier 返回 True 时，请求 ledger 保留不可自动清除的 `outcome_unknown` tombstone；classifier
  自己抛错也按 unknown 保守冻结，不能因分类器故障重新授权同一请求。返回 False 的 clean preflight
  mismatch 才按既有失败处理，不冒充成功 replay。
- Pipeline 直接 catch exact type：unknown 写成耐久 `commit_repair_required` marker，保留 copied
  `commit_facts` 且关闭自动 replay；preflight mismatch 写成 `history_authority_mismatch + reload_required`，
  等 fresh Context 后显式 retry。所有入口都不以 message/reason/code 字符串决定 durability。

## 9. audit gates

注册到 [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md)：

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-UNDO-01` | undo_produces_rollback_op | undo 后 OpLog 末尾 kind=rollback 的 op | runtime |
| `GATE-UNDO-02` | redo_inverts_undo | undo 后 redo 应恢复原数据状态 | runtime |
| `GATE-UNDO-03` | apply_patch_set | apply_patch_directly 处理 set op 正确（CAS 跑）| runtime |
| `GATE-UNDO-04` | apply_patch_meta_ops | apply_patch_directly 处理 create / soft_delete / restore | runtime |
| `GATE-UNDO-05` | group_id_aggregation | 同 group_id 多 op 一次 undo 反所有 | runtime |

## 10. 红线规则 + 反模式

红线：

- **undo 不能绕 CAS** — 必须走 entity.update / delete / restore；不能 `entity._data = ...`
- **不能 mutate OpLog 已有 entry** — OpLog 是 hash-chain append-only；rollback 是**新追加**，不是修改
- **redo_stack 只在耐久提交确认后变化** — prepare_undo 不 push，prepare_redo 不 pop；只有
  `confirm_deferred` 核验已提交 Operation 后才能推进
- **rollback_op.kind 必须是 `"rollback"`** — 用于 OpLog 过滤 + UndoController 跳过这类 op
- **producer 不拥有提交副作用** — prepare 只 apply + 产 Operation；不能 save / append / publish
- **失败不能推进历史** — replay/coverage/cardinality/store 失败由外层 session 恢复；只有 committed receipt 后才能 confirm，失败尝试不造 canonical error Operation
- **bound OpLog 不能 direct append** — 所有 canonical append 必须来自 exact authority 的 active coordinator/store write scope，并逐 Operation 消费一次性 permit；同路径新对象也无权写
- **一个动作不能混用两个 history head** — source selection、redo stale 判断、replay derivation 与 store intent 必须来自同一 transaction 的 fresh snapshot/head
- **unknown outcome 不能 discard/reload/retry 成 clean** — `outcome_unknown=True` 只能 repair/reconcile；只有 source head 被 fresh 证明完全未变化时，transaction 才可 DISCARDED
- **durability 只按 exact type/boolean 消费** — transport、Pipeline 和 idempotency 不得通过异常 message、reason 名称或共同 code 猜测 outcome

反模式：

- **AP-U1** caller 直接调 `entity._data.update(reverse_patches)` 绕过 CAS → 失审计 + version 不 bump
- **AP-U2** prepare 后手工 append 却不经 CommitCoordinator 产 canonical event → 前端看不到回滚且 Document/OpLog 可分叉
- **AP-U3** 用 CommandRegistry.dispatch 做 undo → 撞 sources_allowed；undo 应走 apply_patch_directly
- **AP-U4** 同 group_id 跨大时间窗口的 op 被一起 undo → 应只收集"连续 + 同 group_id"（中间断了即止）
- **AP-U5** redo 后忘了重新 push 进 undo 路径 → redo 完成后 redo_op 自身也是历史，下次 undo 应能反这个 redo
- **AP-U6** 把当前整文档 Checkpoint 描述成“已增量/跨快照去重” → 事实仍是每份 JSON full snapshot；未来 AssetStore 增量化前必须诚实报告容量成本
- **AP-U7** prepare 完就 push/pop redo 栈 → 存储失败后 UI 历史已经前进，重试会指向错误 group
- **AP-U8** 未提交 Operation 就调用 confirm → 控制器必须核验日志长度和尾部 id 并拒绝
- **AP-U9** 新建另一个 OpLog 对象指向相同路径并在合法 write scope append → path alias 不是 exact writer，必须拒绝
- **AP-U10** 捕获 `history_authority_mismatch` 后一律自动重试 → 同一 code 同时承载 preflight 与 unknown；必须读取 exact `HistoryAuthorityError.outcome_unknown`
- **AP-U11** 把 FROZEN authority 从 registry 删除后继续 legacy append → freeze 保留 path ownership；只有 bootstrap 安全 retire + fresh bind 可重建
- **AP-U12** 把 source ticket、trusted head 或 runtime revision 写进 business snapshot → 它们是进程能力/诊断事实，不是用户业务数据，重启只能由 bootstrap 重签

## 11. 当前实装状态

### ✅ 已实装

| 项 | 说明 |
|---|---|
| `UndoController(op_log, doc)` | 双栈构造 |
| `HistoryAuthority` bootstrap binding | Context 发布/recovery 前绑定 exact Document/OpLog/UndoController/CommitCoordinator/Store + project generation/runtime revision；同对象/同路径重复绑定、alias 与 stale capability 拒绝 |
| coordinator history transaction | 每次 execute 固定 fresh trusted head；首次读取缓存同一 rows snapshot；normal/history commit 都逐 Operation 绑定 intent/append permit 并核对 durable suffix |
| `HistorySourceTicket` | undo 只 claim 完整连续 native group，redo 只 claim committed undo；source body→exact replay Operation→REPLAYED→DURABLE→CONFIRMED/DISCARDED 为一次性状态机 |
| `HistoryAuthorityError` | typed `outcome_unknown/durability_phase/commit_facts`；HTTP/MCP/CLI/Pipeline/idempotency 按类型消费，unknown 禁止自动 replay |
| lifecycle / fork | freeze 保留 deny lease；bootstrap-only safe retire；reload 必须 fresh Context；fork child poison inherited object/path 并要求 fresh bind |
| `prepare_undo(actor)` | 从 coordinator 的单一 fresh snapshot apply reverse + 产 Operation；不保存、不 append、不 push/clear redo |
| `prepare_redo(actor)` | 从同一 snapshot peek redo 栈 + apply 原正向 + 产 Operation；不提前 pop |
| `confirm_deferred(record)` | 核验 Operation exact durable suffix/source ticket 与 controller body ledger 后，才 push / pop redo 栈；重复/乱序确认拒绝 |
| `discard_deferred(record)` | 只在 fresh head 证明 storage 未变时释放 DISCARDED source ticket，不冒充 W04 去恢复 Document |
| `undo / redo(actor, persist=...)` | 仅未绑定且无-policy旧调用兼容，内部复用 deferred producer 后自行 persist + append + confirm；bootstrap runtime 必须经 exact coordinator |
| `can_undo / can_redo` | 是否有可撤/可重 |
| `rollback_field(entity, field, target, actor)` 工厂 | 造 kind="rollback" 含 set patch |
| `rollback_entity_to_op(entity, op_log, target_op_id, actor)` 工厂 | 拼累积 reverse_patches |
| `Document.apply_patch_directly(...)` / `apply_patches_atomically(...)` | module-private capability 限定的 Patch 重放；bound runtime 另需 exact source ticket，policy runtime 另需 coverage；走 CAS/审计/容量门、missing/null/ancestry/provenance/position 与最终 cardinality，不走 command ACL；普通 caller fail closed |
| `CheckpointMeta` dataclass | 签名定下 |
| `CheckpointManager.create / restore / list / fork` | 已实现整文档 JSON 快照、命名校验、元数据、列举与派生；restore 返回新 Document |
| undo/redo canonical event | server 生产入口经 CommitCoordinator 提交 rollback Operation，事件由统一提交边界发布 |
| `IdempotencyStore.run(..., outcome_unknown_classifier=None)` | 尾部 keyword-only 加性参数；HTTP/MCP/CLI 传 typed history classifier，unknown 或 classifier failure 留 repair tombstone |
| audit gates 5 个 | GATE-UNDO-01..05 |

### ⏳ 待补（真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| Checkpoint snapshot 增量化 | [03-asset/](../../01-data-structure/03-asset/) 内容寻址做 blob 复用；当前整文档实现继续可用 |
| 跨 Doc undo（多 doc 关联）| 多 Doc 场景启动后 |
| 跨 session 的 undo（持久化 redo_stack）| 业务方明确需求时再加 |

### 原则

- **3.36 兼容边界**：`DeferredHistoryRecord` 旧六个 required 构造参数不变；authority ticket/body ledger 仍是 controller 私有 map，没有新增 public required 字段。未绑定、无 policy 的独立 legacy 对象继续可走旧 helper；一旦 bootstrap 绑定 canonical path，就有意 fail closed，不允许回退成旧直连 writer
- **持久字节兼容**：authority/runtime identity 不进入 Operation/OpLog/business snapshot；新增 Patch envelope 缺省时省略，所以既有 Operation canonical body 与 OpLog hash 不变
- **公开加性参数**：`IdempotencyStore.run` 只在尾部增加默认 `None` 的 keyword-only classifier；未传调用保持旧行为。HistoryAuthority transaction/bind/retire port 都是 framework private，不承诺 mod 直调
- **API 签名先定下来**：UndoController / rollback_* / apply_patch_directly 后续不变签名
- **走 CAS 保审计**：undo 也是变更，actor_log 跟 OpLog 都得有迹
- **kind="rollback" 是分类标签**：OpLog 过滤、前端"撤销"按钮 enabled 状态都靠它
