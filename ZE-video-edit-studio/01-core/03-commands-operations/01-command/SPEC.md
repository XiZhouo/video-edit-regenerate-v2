# Command — 所有动作的统一入口

## 1. 这是什么

任何"会改数据"的动作——业务方写的 mod 命令、framework 自带的 entity 增删改、AI pipeline 调用、远端协作者 sync 进来的变更——都**必须经过 Command**。

业务白话视角：业务同事在 ZE-knowledge-base 点"新增条目"按钮，UI 不能直接调 `doc.add(Entity(...))`；生产入口必须让 `CommitCoordinator.execute(...)` 包住 `cmd_registry.dispatch("entry.create", args, actor=..., source="user", doc=doc)`。Command 内部：
1. 校验 ACL / source / poll
2. 调 entity / document API 做事
3. 造 patches + reverse_patches 装成 Operation；若有派生写/提交后动作，装成 CommandMutation
4. 返回 Operation / CommandMutation 给外层 coordinator

这里刻意不包含“落 OpLog / 保存 Document / 发布 canonical entity_change”。这三件事是一次提交的不同部分，必须由唯一的 CommitCoordinator 统一排序、失败回滚并返回 CommitReceipt。CommandRegistry 只是执行器，不是第二个、偷藏的提交器。W04 起它只额外拥有 handler 级内层 mutation session：execute 抛错或返回非法类型时原位恢复；成功则关闭内层凭据。生产路径的外层 session 仍归 CommitCoordinator，负责跨多次 dispatch 与 store 失败的整笔恢复。

**走 Command 才有的能力**（绕过则全部失去）：
- ✅ undo / redo（reverse_patches）
- ✅ 审计（OpLog 谁干过啥可查）
- ✅ 协作同步（Operation 推到远端，他们 apply patches 跟上）
- ✅ ACL（dispatch 时检查 source / role）
- ✅ poll（按当前状态决定按钮 enabled / disabled）

## 2. CommandSpec — 命令声明

```python
@dataclass
class CommandSpec:
    id: str                              # "entity.update" / "<mod>.<action>"
    label: str                           # 人看的名字 (用于菜单 / 命令面板)
    kind: str                            # native | stage | compose
    phase: int = 1                       # rollout 阶段 (β.X 灰度用)
    touches: list[str] = []              # 影响的 entity_type 列表 (冲突检测 / 索引用)
    conflict_strategy: str = "reject"    # reject | last-write-wins | three-way-merge | crdt
    scheduling: str = "immediate"        # immediate | queued | low-priority
    acl: dict = {}                       # ACL 规则 (后续 04-user-facing/03-authentication 用)
    sources_allowed: tuple = (user, ai, sync, system)  # 哪些 source 可调
    shortcut: str = ""                   # 快捷键 (前端绑定用)
    params_schema: dict = {}             # 命令输入 schema(Command.Describe / UI 表单)
    outputs_schema: dict = {}            # 命令完成后承诺的输出 schema(PipelineEditor)
    ui_ports: dict = {}                  # 输入/输出端口展示声明(PipelineEditor)
    poll: Callable | None = None         # (args) -> bool; 检查能不能执行
    execute: Callable = required         # (args, actor, source, doc) -> Operation | CommandMutation
    mutation_authority: bool = False     # 3.36: 是否可作为受保护 mutation policy 的业务根
```

`mutation_authority` 追加在完整旧 positional surface 的末尾，兼容 Python 3.9 与既有调用；**新代码必须用关键字显式传入**。默认 `False`。它不是“这个命令碰过数据”的自我声明，而是一个窄的安全权限：只有面向业务的 root command 才能设为 `True`，通用 CRUD helper（全部 `entity.*`）保持 `False`。

`register(spec)` 会把 id、handler、poll、source/confirm/AI 风险、schema 声明和 authority 位复制进 framework-owned immutable registration。`get()` 为兼容仍返回公开 `CommandSpec`，但 dispatch、can、confirm、governor 和 policy 验证只消费注册时快照；注册后修改公开对象不能换 handler、放宽 source/确认或提升权限。替换 private map、改绑定对象或让公开 spec 与冻结值分叉都会 `mutation_command_registry_corrupted`，不会静默采用新值。

3.39 的 Pipeline Start 还消费一条更窄的**活 handler 根**。最外层 dispatch 在进入 execute handler 前建立 `_PipelineDispatchRootAuthority`，嵌套 helper 只能共享同一对象；poll、governor 与确认准备拿不到它。handler 可通过 framework-private seam 取得唯一 snapshot，但 sealed factory 消费时必须重新确认 dispatch stack 仍活着、同一 registry/Document、同一冻结 registration object/digest、同一 mutation-policy attestation、精确 document/run/pipeline identity，以及排序去重后的目标类型都存在于当前 schema 与受保护 policy 中。全部检查完成后才把 root 单向置为 consumed。actor/source、root command digest 和 document policy 只能从该对象投影，factory 不接受 caller 自填的替代权威。snapshot 与 verified projection 都不可复制/序列化，fork child 因 dispatch stack 清空而失效。这条根只允许签发一次 Start Operation；它不是 durable fact、attestation 或 worker grant，不能保存到 manifest 后恢复。

3.40 又增加一个只给 Pipeline Batch 4 使用的 **delegated root revalidation** helper。它处理的是“Start 已耐久以后，当前业务根是否仍然成立”，不是在新线程里复制一次 dispatch：

1. 必须传入 exact `CommandRegistry` 和已 seal 的 exact `Document`，command id 必须仍绑定当前 registry 中同一个 `mutation_authority=True` 的 business registration；
2. 当前 registration declaration digest 必须逐字等于 Start 中的 root digest，registry epoch/pid/command attestation 也必须仍被 Document 接受；
3. helper 只从当前 Document schema 与 mutation policy 重建 scoped、detached document-policy projection。Pipeline seam 再用与 Start codec 同源的 canonical digest 比对 Start 中的 policy digest，避免 Command 和 Pipeline 各写一套摘要算法；
4. helper 不调用 command handler、poll/governor 或 nested helper，不建立 dispatch stack，不返回 Operation/CommandMutation，不改 Document，也不签 grant。它只是一次 passive policy projection/revalidation；未来真实 worker 仍必须在 CommitCoordinator 内经过 3.36 actual-delta 与 exact reversible Operation coverage；
5. id 以 `entity.*` 开头的通用 CRUD 在 Start 签发和 delegated revalidation 两处都固定拒绝。它们可以是真实业务 root handler 内的受约束机械 helper，不能升格为授权根或 delegated primary Operation id。

这个 helper 只被 core 3.40 的 dormant request prepare/consume 调用；当前没有 protected worker 会消费它的返回值。它不能被公开业务代码当作“不 dispatch 也能写”的捷径，也不能代替 Batch 5 的 grant registry、Control lifecycle 和 dynamic creation registry。

**kind 三态**：

| kind      | 含义            | 典型场景                                   |
| --------- | ------------- | -------------------------------------- |
| `native`  | 单步原子命令        | entity.add / entity.update / 简单业务动作    |
| `stage`   | 长任务的某一阶段      | AI pipeline 中间步骤（progress 可见 / 可取消）    |
| `compose` | 把多个 native 组合 | "复制选中 3 个 entity" = 3 个 entity.add 串起来 |

**硬规则:慢/外部调用必须 stage,不得塞进 native**（门禁 `scripts/command_async_check.py` 守）：
- `native` 是**毫秒级同步原子动作**。其 `execute` **禁止**做慢/外部调用 —— 模型调用
  (`llm_call`/`image_call`)、网络出网、长 IO 都不行。
- 任何会跑几秒~几十秒的动作(典型:AI 生成 / 出图)**必须** `kind="stage"`,经
  `PipelineSupervisor` 跑(见 [03-pipeline/SPEC.md](../03-pipeline/SPEC.md))。**只有 stage 路径**才点亮
  v3 的在途反馈:实体加载态(`EntityRunStatus`→`entity_status` SSE→编辑器骨架/进度)、footer 独立
  管线面板(进度条 / ETA / 单步暂停·续跑·取消 / 重试)、跨进程续跑、资源闸门、崩溃安全。
- 反例:`@registry.command(kind="native") def gen(...): ... llm_call(...) ...` —— 假装瞬时,
  阻塞请求、无加载态、不可暂停/重试。正例:`item.batch_generate`(`kind="stage"` + `supervisor.start`)。
- stage **handler**(传给 `StageSpec` 的 `run`)里调 `llm_call`/`image_call` 是**对的**(它本就是
  慢任务的执行体);受约束的只是 `@registry.command` 装饰的 `execute`。

**touches**：声明本命令会影响哪些 entity_type。用途：
- 冲突检测：A B 同时执行 → touches 重叠才需要走冲突策略
- 索引建立：知道哪些 entity 会被频繁改 → 上层建索引/缓存

**conflict_strategy 四态**：
- `reject`（默认）— CAS 冲突直接拒，让 caller 决定
- `last-write-wins` — 后到的覆盖（适合实时同步无关紧要的字段）
- `three-way-merge` — 用 base + A + B 三方合并（CRDT 场景）
- `crdt` — 用 CRDT 数据类型自动合并

本叶子**仅记录 conflict_strategy 字段**，真正合并逻辑在 [02-data-flow/02-conflict-resolution/](../../02-data-flow/02-conflict-resolution/) 叶子。

**scheduling 三态**：
- `immediate` — 立即执行
- `queued` — 排队（长任务 / 限流场景）
- `low-priority` — 后台低优先级（备份 / GC 类）

调度逻辑在 [02-data-flow/](../../02-data-flow/) 叶子。本叶子**仅记录 scheduling 字段**，调度内核未实装。

**sources_allowed**：哪些 source 能调本命令。例如：
- `entity.hard_remove` 设 `("user", "system")` — 拒绝 ai/sync 直接硬删
- `pipeline.ai-generate` 设 `("user", "ai")` — 拒绝 sync/system 触发 AI

对 mutation-policy 受保护的真实变化，source 约束还会与 Actor/Operation 对账：dispatch 开始时冻结 exact `Actor.to_dict()`；`Actor.source` 必须等于 dispatch 的 source；Entity 新增 actor-log 以及 root/helper 返回的每条 Operation 的 actor/source 都必须相同。嵌套 dispatch 不能换 actor/source，callback 修改 caller 或 handler Actor 会恢复原值、回滚并拒绝。

**poll 函数签名**：`(args: dict) -> bool`。前端展示菜单 / 按钮 / 命令面板时调 poll 决定 enabled。比如 `entity.delete.poll = lambda args: doc.find(args["type"], args["id"]) is not None`（entity 存在才能删）。

**execute 函数签名**：`(args, actor, source, doc) -> Operation | CommandMutation`
- `args` dict — 命令参数
- `actor` Actor — 触发者
- `source` str — user/ai/sync/system
- `doc` Document — 操作对象
- 返回 Operation，或返回包含有序 Operations + DeferredActions 的 CommandMutation

**Pipeline notebook 输出契约**:
- `params_schema` 继续表示命令输入。
- `outputs_schema` 表示命令/command cell 完成后对外承诺的输出,例如:
  `{"guideRef":{"type":"asset","mime":"image/png","label":"Guide image"}}`。
- `ui_ports` 告诉 `pipeline` editor 如何展示输入/输出,例如:

```json
{
  "inputs": [
    { "id": "source", "kind": "image", "source": "entity_field", "field": "sourceRef" }
  ],
  "outputs": [
    { "id": "guide", "kind": "image", "source": "stage_output", "field": "guideRef" }
  ]
}
```

旧命令不声明这两个字段时保持兼容;pipeline editor 会回退显示 command label、stage 状态、日志和 artifact 摘要。

## 3. CommandRegistry — 注册 + dispatch

```python
class CommandRegistry:
    def register(spec: CommandSpec) -> None              # id 冲突抛 ZeError
    def get(command_id: str) -> CommandSpec              # 不存在抛 ZeError
    def list() -> list[CommandSpec]
    def has(command_id: str) -> bool
    def is_mutation_authority(command_id: str) -> bool   # 注册时冻结的 authority 位
    def registered_declaration(command_id: str) -> dict  # detached 注册时生产投影
    def registered_declarations() -> list[dict]          # 按注册顺序返回全量 detached 投影
    def can(command_id, args) -> bool                    # 调 spec.poll 检查
    def dispatch(
        command_id, args, *,
        actor: Actor, source: str, doc: Document,
        op_log: OpLog | None = None,              # deprecated; 兼容接收但忽略
    ) -> Operation | CommandMutation
    def command(**kwargs) -> Decorator                   # 装饰器: @reg.command(id=..., kind=...)
```

**dispatch 流程**：

```
caller 进入 commit_coordinator.execute(producer)
   ↓
1. producer 调 cmd_registry.dispatch("entry.update", ...)
   ↓
2. 读取 frozen registration                    # 不存在 → ZeError("unknown_command")
   ↓
3. 解析最外层业务命令身份与 nested scope       # nested 只能同 registry + 同 Document
   ↓
4. 开内层 mutation session，执行 ACL/poll/governor/确认准备
                                               # preflight 必须只读；pending 返回前也回滚 session
   ↓
5. mutation = frozen.execute(args=args, actor=actor, source=source, doc=doc)
                                              # execute 内部做事 + 造 patches + new_operation
   ↓
6. policy opt-in 时校验真实 graph delta、最外层 command 权限、
   primary audit identity、forward/reverse patch 精确可逆与最终 cardinality
   ↓
7. return mutation 给 coordinator              # dispatch 本身无持久化 / 无发布副作用
   ↓
8. coordinator 验整笔 coverage 后才触达 store、OpLog 与 canonical event
```

`CommandRegistry(broker=...)` 与 `dispatch(..., op_log=...)` 的参数暂时保留，让已生成的 mod 和 bootstrap 可以分步升级。它们都是**无副作用的兼容参数**：broker 不会被 publish，op_log 不会被 append。不允许任何调用方依赖这两个旧行为。

如果 Document 没有任何 mutation policy，上述新增授权/coverage 路径保持关闭，既有命令语义不变。若有 policy，handler 返回的 Operation 不能“自证清白”：授权只看最外层真实 dispatch id，审计只接受能把真实 snapshot 正向变成 final、再反向恢复 snapshot 的 patches。组合命令可以把第一条 Operation 作为业务 root，后续 helper Operation 承载细节 patch；但不能丢弃某次 dispatch 的 Operation，也不能多报未发生的受保护修改。

policy 引用闭包必须在 framework + mod commands 全部注册后、任何请求与 pipeline recover 前显式执行：

```python
register_builtin_commands(commands)
mod_commands.register(commands)
doc._validate_mutation_policy_commands(commands)  # framework bootstrap only
```

这一步要求每个 policy command id 同时满足“真实存在”与“注册时 `mutation_authority=True`”。仅把字符串写进 policy、在注册后改 `commands.get(id).mutation_authority`，或试图提升 `entity.update` 都会拒绝。业务根复用 generic helper 的正确形状是：root handler 在同一 registry/Document/actor/source 下 nested dispatch；返回 `CommandMutation((root_operation, helper_operation, ...))`，其中第一条 Operation 的 command_id 必须等于 root id，nested Operation 必须保持原对象、原顺序与原 body。

成功校验还会把 Document 从“未发布 bootstrap/migration 准备态”单向 seal。seal 后不能用 runtime 私有安装 seam 原位替换 policy；受保护类型的底层 `Entity.update/delete/restore` 与 `Document.add/hard_remove` 会再次要求外层 CommitCoordinator coverage 和当前业务 command authority，两者缺一就在变化前拒绝。history replay 只能改由 coordinator-owned replay coverage 进入。这个 fence 是正常框架路径的同进程误用边界，不是抵御 hostile Python 任意 import 私有符号或篡改内存的 sandbox。

严格校验按实际受保护片段而不是全局开关启用：未受保护类型的真实修改保持旧语义；`mutation_authority=True` 但没有真实/声称受保护变化时不额外扩权。反过来，即使 Document 没有真实 delta，只要返回的 patch 声称触碰受保护类型，也会进入 coverage 校验并拒绝 forged patch。一个 command 同时修改受保护与未受保护类型时，受保护部分必须精确审计，未受保护部分继续按旧合同处理。

create 是组合授权：root 必须列在 `lifecycle.create`；新 Entity 的 metadata name 非空时还要列在 `lifecycle.rename`；初始 data 每个实际字段都要列在对应 field。dot-path 的权限查第一个顶层根，但 patch/provenance 保留原始路径。read-only overlay 永远不可授权。合法的幂等 delete/restore no-op 仍先做 CAS/read-only 校验，返回空 patch且不 bump Entity version；普通同值 update 仍可能是真实 CAS/actor-log mutation，不能被任意省略。

`confirmed=True` 是 confirm 状态机内部的重入形状，不是公开捷径。只有 `confirm()` 在 reservation、actor、source 和冻结命令全部匹配后签发的一次性 thread-local grant；dispatch 在任何回调前消费。直接传 true、跨 Document/actor 复用、nested 重入或 fork 继承都会拒绝。

**装饰器**：

```python
@registry.command(
    id="entry.create",
    label="新建知识条目",
    kind="native",
    touches=["entry"],
    mutation_authority=True,  # 仅当 policy 引用这个业务根时
)
def create_entry(args, actor, source, doc):
    helper = registry.dispatch(
        "entity.add",
        {"type": "entry", "data": args["data"]},
        actor=actor, source=source, doc=doc,
    )
    root = new_operation(
        command_id="entry.create",
        source=source,
        actor=actor.to_dict(),
        patches=(),
        reverse_patches=(),
    )
    return CommandMutation((root, helper))
```

generic `entity.add` 负责生成完整 Entity envelope、exact canonical `position` 和倒序正确的 reverse。直接手写一个没有 `position` 的 create/delete Patch 不能通过受保护 coverage。

## 4. 内置 command（9 个 entity.* + 2 个 document.* + notify.push）

framework 自带，业务方可直接 dispatch（覆盖 entity 增删改基础操作）。

| command_id | args | 调用的 doc/entity API | patches / reverse 形态 |
|---|---|---|---|
| `entity.add` | `{type, data, name?, id?, schema?, read_only?}` | `doc.add(Entity(...))` | create → delete |
| `entity.batch_add` | `{items:[{type, data?, name?, id?, schema?, read_only?}]}` | 逐项 `doc.add(Entity(...))` | 单 Operation 顺序 create；reverse 倒序 delete |
| `entity.update` | `{type, id, patches, expected_version, name?, ai_provenance?}` | `e.update(...)` | 每字段 set；reverse 互换 before/after |
| `entity.batch_update` | `{type, items:[{id, patches, expected_version}]}` | 逐项 `e.update(...)` | **单 Operation 覆盖 N 实体**(每实体每字段 set;undo 一键回滚)。整体拒绝语义:先全量预检 CAS 版本(任一不匹配抛 `cas_version_mismatch`,doc 零变更);apply 途中其余异常(read_only/软删/schema)→ 已应用项按 before 回滚后原样抛。消费者:form 多选聚合编辑;cardlist rename/crop 批量(残债承接) |
| `entity.reorder` | `{type, id, before_id?, ai_provenance?}` | 归一同 type 的 `order` 字段，内部以每个 Entity 当前版本执行 CAS | 单 Operation 多字段 set；reverse 按真实执行倒序 |
| `entity.delete` | `{type, id, expected_version}` | `e.delete(...)` | soft_delete → restore |
| `entity.restore` | `{type, id, expected_version}` | `e.restore(...)` | restore → soft_delete |
| `entity.hard_remove` | `{type, id}` | `doc.hard_remove(...)` | create → delete（reverse 恢复整 entity）|
| `entity.cascade_delete` | `{type, id}` | `doc.cascade_delete(...)` | 整组 soft_delete patches |
| `document.rename` | `{name, expected_version}` | `doc.rename(...)` | `__document__` 级 name set；必须用用户已渲染的 Document 版本做 CAS |
| `document.set_acl` | `{acl, expected_version}` | `doc.set_acl(...)` | `__document__` 级 ACL set；owner 授权由 server 校验，所有 surface 都强制 Document CAS |
| `notify.push` | `{recipient, message, channel?, template?, idempotency_key?}` | 只产 pending 外发意图；真实 provider 在 after-commit action | 无 Document patch；Operation 与 ActionReceipt 分层 |

**关于 document.* 命令**:Header SPEC §2.3.5 列了 new/open/save_as/snapshot/export/close/delete/rename 一组文件菜单命令；严格按 v3 体系，内置的 doc-meta mutation 只有 `document.rename` 与 `document.set_acl`：
- **rename / set_acl**:改当前文档元数据 → 经 `__document__` Patch 走标准 Operation/OpLog/广播；两者都必须带用户已渲染的 `expected_version`，不能 last-write-wins。
- **new / open / save_as / close / delete**:都是"多文档切换/复制/销毁",触碰 [01-document SPEC §8 红线「一律单 Document」](../../01-data-structure/01-document/SPEC.md) → 属工作台/cross-skill 层,**不在单 skill 内置**。
- **snapshot**:属 [04-history-rollback](../../02-data-flow/04-history-rollback/) 的 CheckpointManager；当前整文档 JSON 快照已实装，后续可替换为内容寻址增量。
- **export**:只读副作用(序列化),不产 entity 变更;可作只读端点,非内置 mutation command(待补)。

**注意**：
- `entity.hard_remove` 的 reverse 是 create + 还原所有字段；但物理删后历史已丢，reverse 只能还原**删除时的 snapshot**
- `entity.cascade_delete` 产生**多 entity 的 patches**——一次 op 含多个 entity 的变更
- 内置 command 不含 ACL 默认值（`acl={}`），且全部 `entity.*` 固定 `mutation_authority=False`。它们可直接服务未受保护类型，也可被受保护业务根嵌套消费，但不能成为 policy 权限根
- command id 注册后不可覆盖；mod 要增加 ACL 或业务语义必须使用自己的 id，再在 handler 内调用内置 helper

业务方 mod 端**可以**调用这些内置 command；**也可以**自己注册更高层的复合 command。

## 5. 强制纪律：mod 变更必走 Command

**所有 mod 端业务代码的变更必须经 Command，不允许在 command handler 外直接调 doc / entity 的变更 API**。

| 必须走 Command 的事 | 不允许直接调 |
|---|---|
| 新增 entity（增 entity_type 实例）| ❌ `doc.add(Entity(...))` 在 mod handler 外 |
| 改 entity 字段 | ❌ `entity.update(...)` 在 mod handler 外 |
| 软删 / 恢复 / 硬删 / 级联删 | ❌ `entity.delete` / `doc.hard_remove` 等 |
| asset 生命周期维护 / GC | ❌ 在 producer 内 `add_ref/release/reconcile/gc`；提交后按 canonical Document 全量对账 |

**为什么强制**：
- **undo**：不走 Command 没 reverse_patches，撤不回来
- **审计**：不走 OpLog 查不到"谁干的"
- **协作同步**：不走 Operation 推不出去，其他端看不到变化
- **ACL 兜底**：不走 dispatch 没 source / role 检查

**framework 内部还能用 public API 吗**：能，但不是“只要写在任意 handler 里就自动有权”。未受保护类型保持旧兼容；Document seal 后，受保护 CRUD 只有在 CommitCoordinator producer 内、当前真实业务 command segment 已取得 authority 时才可用。推荐业务 root 嵌套 `entity.*` helper，由 helper 统一生成 patches；history 则走专属 replay coverage。未发布的 bootstrap/migration 准备态可在 seal 前由 framework seed/迁移，不能开放给 public session。

**asset 为什么不随 handler 增减 refs**：Document mutation 可以被 W04 session 原位回滚，文件 meta 不能；而 import、duplicate、undo、redo 也不都经过 add/update/hard_remove handler。内置 producer 因此不写引用账，`AssetStore` 在已提交后的维护边界扫描完整 Document（含软删和 read_only）统一 reconcile，GC 也必须先做同一遍扫描。producer 中只有 `put(bytes)` 可作为不可变内容准备；失败最多留下 orphan，不会成为 GC 权威。其余 asset 写由 phase guard 拒绝，W06 再补多进程锁与 orphan TTL sweeper。

**实操上**：lint / code review / 审计 gate 仍负责所有类型的结构纪律（例如扫 mod 代码不出现 handler 外 `doc.add`）。此外，mutation-policy 受保护类型在 seal 后已有运行时 hard fence：缺 coordinator coverage 或 command/history authority 的直接写会 fail closed。它缩小同进程误用面，但不替代 Python 进程自身的信任边界。

## 6. Python API

```python
from command import CommandMutation, CommandRegistry, CommandSpec
from operation import new_operation
from entity import Actor
from document import Document

# ──────── 注册（mod 端写 register() 函数）────────
registry = CommandRegistry()

# 方式 A: 装饰器
@registry.command(
    id="entry.create",
    label="新建条目",
    kind="native",
    touches=["entry"],
    mutation_authority=True,  # policy 引用的显式业务根才设置
)
def create_entry(args, actor, source, doc):
    helper = registry.dispatch(
        "entity.add", {"type": "entry", "data": args["data"]},
        actor=actor, source=source, doc=doc,
    )
    root = new_operation(
        command_id="entry.create",
        source=source, actor=actor.to_dict(),
        patches=(), reverse_patches=(),
    )
    return CommandMutation((root, helper))

# 方式 B: 显式 register
spec = CommandSpec(id="x.y", label="...", kind="native", execute=fn)
registry.register(spec)

# ──────── 查询 ────────
registry.get("entry.create")                     # ✅ CommandSpec; 不存在抛 unknown_command
registry.list()                                  # ✅ [CommandSpec]
registry.has("entry.create")                     # ✅ bool
registry.is_mutation_authority("entry.create")   # ✅ 注册时冻结的 bool
registry.registered_declaration("entry.create")  # ✅ detached frozen registration projection
registry.registered_declarations()                # ✅ 注册顺序；生产 Describe/list 使用
registry.can("entry.create", args)               # ✅ 调 spec.poll; 无 poll 默认 True

# ──────── dispatch ────────
execution = commit_coordinator.execute(
    lambda: registry.dispatch(
        "entry.create",
        args={"data": {"title": "x"}},
        actor=Actor(user_id="u1"),
        source="user",
        doc=doc,
    )
)
# coordinator 先开 coverage，再运行 dispatch；execution.receipt 决定是否 committed。
```

## 7. 内置 commands 注册

framework 启动时调 `register_builtin_commands(registry)`：

```python
from command import CommandRegistry
from builtin_commands import register_builtin_commands

registry = CommandRegistry()
register_builtin_commands(registry)
# 9 个内置 entity command 已就位（全部 mutation_authority=False）:
#   entity.add / entity.batch_add / entity.update / entity.batch_update /
#   entity.reorder / entity.delete / entity.restore /
#   entity.hard_remove / entity.cascade_delete
# 另有 document.rename / document.set_acl / notify.push。
```

mod 端在 `02-mod/05-code/commands_register.py` 写自己的 register()，跟内置 commands 在同一个 CommandRegistry 共存。

## 8. ACL 基础

**当前**：本叶子只验 `sources_allowed`。具体 acl 字段（role / user_id 白名单）解析逻辑在 [04-user-facing/03-authentication/](../../04-user-facing/03-authentication/) 叶子完成。

**用法**：

```python
@registry.command(
    id="admin.hard-delete-all",
    sources_allowed=("user",),                   # ai / sync / system 不许调
    acl={"required_role": "admin"},              # 04-authentication 完成后解析
    ...
)
```

dispatch 时如果 source 不在 sources_allowed 抛 `acl_denied`。`acl` 字段当前是占位 dict，存进 spec 等后续叶子用。

## 9. 跟提交层的集成

dispatch 不再直接集成 OpLog 或 realtime broker。CommitCoordinator 先包住 producer；producer 内的 dispatch 返回 Operation / CommandMutation 后，由同一个外层继续处理：

1. 确认命令 handler 已产生合法 Operation，或 Operation 有序集合 + DeferredAction；
2. 按保存策略耐久化 Document 和 OpLog；
3. 只在提交事实已确定后，将 canonical entity_change 写入 outbox / 发布通道；
4. 返回可审计的 CommitReceipt，明确 committed / dirty / aborted / indeterminate 与是否可安全重试。

Command 与 Commit 的失败语义只有一套：handler、poll、governor、确认准备、policy/coverage 或返回类型校验失败时，dispatch 抛异常并关闭内层 mutation session；它不保存、不 append、不 publish。CommitCoordinator 的外层 session 再覆盖多 dispatch、store 与事件提交边界：可证明未 durable 的失败恢复整个 Document；durable/unknown 则按 receipt 进入修复，绝不先盲目 rollback 再自动重放。canonical OpLog 只接收实际提交的 Operation；失败尝试若需审计，使用独立 failure-attempt 记录，不把 `Operation.error` 当成吞异常的通道。

确认门控也不例外，但 token 的消费必须晚于提交事实。生产路径固定为：

1. `PendingStore.reserve(token, actor, approval_channel, approval_nonce)` 原子校验 actor、人工通道和一次性 nonce，并取得唯一 reservation；
2. `CommandRegistry.confirm(reservation, ...)` 用冻结参数重跑 ACL/poll，只执行 handler；
3. `CommitCoordinator` 返回后，`PendingStore.finalize` 读取同一 CommitReceipt：
   - `committed`（即便 event failed）→ consume；
   - `aborted` 且 `rollback_succeeded=true` → release，可再次确认；
   - `dirty` / `indeterminate` / 回滚失败 → freeze，后续固定报
     `confirmation_repair_required`，禁止重放。

HTTP 确认必须使用 `sdk.persistence.execute_confirmation`，MCP 只能创建/取消 pending，不得自批。取消也要校验
创建 token 的 actor，且只能取消 pending；reserved 返回 `confirmation_in_progress`，frozen 返回
repair-required。公开 pending 投影不含 args/actor，只有网页人工通道拿到 approval nonce。
当前 store 是带 TTL/容量上限的 project 内存实现，W06 把同一状态机与 commit 放入数据库事务。

**典型 framework 启动结构**：

```python
doc = Document(...)
registry = CommandRegistry()
register_builtin_commands(registry)
mod.register(registry)                            # mod 端注册自己的 command
op_log = OpLog()

# policy 引用闭包通过后单向 seal；必须早于 recover / 开放请求
doc._validate_mutation_policy_commands(registry)  # framework bootstrap only

# 后续所有变更：coordinator 必须包住 producer/dispatch
execution = commit_coordinator.execute(
    lambda: registry.dispatch(cmd_id, args, actor=..., source=..., doc=doc)
)
```

### 9.1 `notify.push`：外发意图与发送事实分层

`notify.push` 是框架对不可回滚外部 I/O 的参考实现：

1. producer 只读配置/用户名册，校验 channel、recipient policy 和消息非空；
2. producer 构造 `CommandMutation(operations=(intent_op,), after_commit=(send_action,))`；
3. `intent_op.external` 只允许记录无密钥 endpoint 和 `status="pending"`，此时没有 message id、延迟或成功状态；
4. 滑动窗口额度、`EffectLedger.run`、真正 notify provider 与 `NotifyLog` 只能在 `send_action.run(commit_id)` 内发生；
5. Operation / canonical event 不包含 message、idempotency key 或任何 secret。EffectLedger 只耐久请求 hash 和无密钥响应。

故障语义必须符合下表：

| 业务存储 | canonical event | 限流 / ledger / provider / NotifyLog | 业务 receipt |
|---|---|---|---|
| 未 committed | 不发布 | 全部 0 次 | 保持 aborted / dirty / indeterminate 的真实分类 |
| committed | failed | 全部 0 次 | committed，event failed |
| committed | published / durably pending | 按顺序执行一次 action | committed |
| committed | published / durably pending，action 失败 | 记一条 failed ActionReceipt | 仍为 committed，禁止重放业务命令 |

此分层刻意不把 provider 结果回填进已耐久 Operation：该 Operation 是意图审计，NotifyLog / ActionReceipt 才是执行审计。W05 的 `DeferredAction` 仍仅存于当前进程，无法覆盖“提交后、执行前崩溃”。W06 需要用 transactional outbox / durable action journal 保存动作意图、幂等键、尝试次数与最终结果。

## 10. 红线规则 + 反模式

红线：

- **mod 代码不能在 command handler 外直接 mutate entity / doc** — 必须 dispatch（AP-C1）
- **execute 必须返回 Operation / CommandMutation** — 不能返回 None / 其他类型（AP-C2）
- **execute 函数签名固定** — `(args, actor, source, doc) -> Operation | CommandMutation`，不能改（AP-C3）
- **不可回滚副作用必须延后** — 启停线程、外部 I/O、manifest、运行态广播只能放进 DeferredAction；producer 内递归提交会被拒绝
- **不能重复注册同 id** — register 抛 `validation_failed`
- **dispatch 不能跨 doc 实例** — 一个 dispatch 调用对应一个 doc；多 doc 操作走 cross-skill-call
- **dispatch / confirm 不得提交** — 不 append OpLog、不保存 Document、不发 canonical entity_change
- **caller 不得手工拼提交** — 不得先裸 dispatch 再自行 save + append + publish；必须让 CommitCoordinator 从 producer/dispatch 之前就包住整次尝试
- **`entity.*` 不得成为 Pipeline 授权根** — 通用 CRUD 只能被真实业务 root 嵌套消费，不能作为 Start/delegated root 或 worker primary Operation 绕过 policy

反模式：

- **AP-C1** mod 业务代码：`def my_handler(): doc.add(Entity(...))` → 应走 `cmd_registry.dispatch(...)`
- **AP-C2** execute 函数忘记 return Operation / CommandMutation → caller 拿到 None 崩；OpLog 没记录
- **AP-C3** execute 自定义参数名（不是 args/actor/source/doc）→ dispatch 调失败
- **AP-C4** poll 函数副作用（改数据）→ poll 应该是纯查询，UI 频繁调 poll 决定按钮状态
- **AP-C5** execute/poll/governor 失败后 catch 成 `Operation.error` 并 return → canonical OpLog 会把一次失败伪装成可提交 mutation；正确做法是原样抛错，让 mutation session 回滚。失败尝试需要审计时写独立 failure-attempt audit，不能混入成功 hash-chain
- **AP-C6** mod 试图注册同 id 覆盖内置 command → register 会拒绝；使用自己的业务 id，在 handler 内 nested dispatch 内置 helper
- **AP-C7** 认为传了 `op_log` / `broker` 就已提交 → 这两个参数只是兼容占位，不产生任何副作用

## 11. 当前实装状态

### ✅ 已实装

| 项 | 说明 |
|---|---|
| `CommandSpec` dataclass | 旧字段 + 末尾兼容字段 `mutation_authority=False`；新代码用关键字显式设置 |
| `CommandRegistry` | register / get / list / has / is_mutation_authority / registered_declaration(s) / can / dispatch / command 装饰器；执行、安全、生产 introspection 与 authority 以注册时冻结 snapshot 为准，旧 get/list 只作兼容 |
| `dispatch` 流程 | source/Actor 冻结 + poll/governor/confirm 只读 preflight + execute + 实际受保护 delta 授权 + exact Operation coverage + 返回 Operation / CommandMutation；失败原位回滚并抛出 |
| 提交隔离 | dispatch / confirm 不 append OpLog、不发 canonical entity_change；broker/op_log 只兼容接收 |
| 9 个内置 entity command | `add` / `batch_add` / `update` / `batch_update` / `reorder` / `delete` / `restore` / `hard_remove` / `cascade_delete`；全部不是 policy authority |
| 其余内置 command | `document.rename` / `document.set_acl` / `notify.push` |
| `register_builtin_commands(registry)` | 一键注册全部 12 个内置 |
| runtime mutation policy | 显式业务 root、per-type opt-in、registry validate 后单向 seal、受保护 CRUD coordinator/command hard fence、nested operation ledger、actor/source、真实 delta、missing/null/ancestry/provenance/position 与最终 cardinality 在 pre-store 回滚边界内强制 |
| Pipeline delegated root revalidation | core 3.40 private helper 重验 exact registry/Document、business authority、registration digest 与 policy attestation，只返回 detached 当前 policy projection；不调 handler、不 dispatch、不 mutate、不签 grant，`entity.*` 始终不能作为 root |
| ZeError 共享 | 从 `entity` 模块 import（跟 document / asset / operation 一致）|

### ⏳ 待补（真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| `conflict_strategy` 真合并逻辑（CRDT / 3-way merge）| [02-data-flow/02-conflict-resolution/](../../02-data-flow/02-conflict-resolution/) |
| `scheduling` 调度内核（queued / low-priority）| [02-data-flow/](../../02-data-flow/) |
| `acl` 真校验（role / user_id 白名单解析）| [04-user-facing/03-authentication/](../../04-user-facing/03-authentication/) |
| stage 命令（长任务进度可见 / 可取消）| [02-data-flow/](../../02-data-flow/) + UI |
| compose 命令（多 native 串成一个 group_id）| 设计已就位（group_id），待按需扩展 |
| 跨 skill 调用（A skill 调 B skill 的 command）| [04-cross-skill-call/](../04-cross-skill-call/) |
