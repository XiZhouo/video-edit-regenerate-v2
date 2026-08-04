# 01-command — Command

> 一句话：**所有数据变更的统一执行入口**。生产写入由 `CommitCoordinator.execute(producer)` 包住 `cmd_registry.dispatch(...)`，让同一次尝试产生 Operation、落文档、写 OpLog 并发布事件。mod 业务代码不允许在 command handler 外直接调 `doc.add` / `entity.update`。

## 在框架里的位置

- 上一级：[03-commands-operations/](../) — 指令与操作 5 个叶子
- 同级：[02-operation/](../02-operation/)（本叶子产生 Operation；CommitCoordinator 在提交时写入 OpLog）
- mod 配套：[`../../../02-mod/03-commands.md`](../../../02-mod/03-commands.md) — 业务方声明本 skill 的 command 清单

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览）|
| [SPEC.md](./SPEC.md) | 11 节完整规则 |
| [command.py](./command.py) | CommandSpec + CommandRegistry + dispatch + 装饰器 |
| [builtin_commands.py](./builtin_commands.py) | 9 个 `entity.*` + 2 个 `document.*` + `notify.push`，由 `register_builtin_commands` 一键注册 |
| [mutation_operation_audit.py](./mutation_operation_audit.py) | 命令层的受保护 Operation 精确审计；单向消费 Document graph primitive 与 Operation carrier，Document 不反向 import 本模块 |
| [test_command.py](./test_command.py) | pytest 基础命令测试 |
| [test_mutation_policy.py](./test_mutation_policy.py) | policy 声明、真实 delta、可逆 patch、cardinality、history 与 coordinator 集成矩阵 |
| [test_mutation_policy_security.py](./test_mutation_policy_security.py) | authority/coverage、跨线程、注册后篡改、pre-session laundering 与 pre-store 回滚对抗测试 |

## 当前状态

**✅ 已实装**（全部无跨叶子依赖项）

- `CommandSpec` dataclass — 执行、展示、确认、AI 治理、输入输出 schema、source/ACL 与调度声明，以及 3.36 末尾追加的 `mutation_authority`（默认 `False`；新代码只用关键字显式设置）
- `CommandRegistry` — register / get / list / has / can / dispatch + `@command` 装饰器；3.36 增加 `registered_declaration(s)`，给 Command.Describe、server label 与 Pipeline 等生产消费者返回 detached 的注册时真值
- 注册时冻结执行/安全权威 — dispatch、poll、confirm、AI governor 与 mutation policy 消费 framework-owned snapshot；注册后改公开 `CommandSpec` 不能换 handler、放宽 source/确认或提升 authority，绑定损坏会 fail closed
- Pipeline Start root（3.39）— outer handler 活着时，registry 才提供一次性 opaque snapshot；sealed factory 消费前重新核对同一 registry/Document、注册对象与摘要、policy attestation、actor/source、目标类型 schema/policy，成功后永久置 consumed。snapshot/verified root 不能复制、序列化或跨 fork 使用，也不接受 caller 自填 actor/source/digest；它只授权本次 Start 描述符签发，不是 worker grant
- Pipeline delegated root 重证（3.40）— dormant request 的 prepare/consume 会调用 framework-private helper，重验当前 registry 中同一 business command 仍是冻结的 mutation authority、registration digest 仍等于 Start root digest，且 Document 仍接受同一 registry/policy attestation；然后只从当前 schema + mutation policy 重建 detached document-policy projection。helper 不调 handler、不进入 dispatch、不 mutate Document、不签发 command authority/grant。`entity.*` 固定不能作为 Start 或 delegated root，也不能把通用 helper 冒充本次 primary Operation
- 生产 introspection 不读 mutable `get/list` — 这两个旧 API 继续返回公开 `CommandSpec` 以兼容 caller；生产标签、schema、sources、确认与 authority 必须读 `registered_declaration(id)` / `registered_declarations()`，每次返回 detached 投影
- dispatch 流程 — `source_allowed` 校验 → `poll` → `execute` → 返回 Operation / CommandMutation
- W03 提交边界 — dispatch / confirm **不写 OpLog、不写文档存储、不发 canonical `entity_change`**；全部交给 CommitCoordinator
- W04 handler 回滚边界 — Document 支持 mutation session 时，dispatch 在 execute 前捕获内层快照；handler 抛错或返回非法类型会原位恢复再抛错。生产调用方外层 CommitCoordinator 仍负责多 dispatch / store 失败的整笔回滚
- 3.36 runtime mutation authority — 对声明了 policy 的类型，dispatch 用最外层真实业务 command id 校验实际 Document 前后差异；该业务根必须注册为 `mutation_authority=True`，并要求 forward/reverse patches 与真实受保护 delta 的身份、顺序和值精确可逆。嵌套 helper、伪造返回 id、漏 patch、伪 patch、直接 `confirmed=True` 均不能扩权
- policy activation fence — declaration 只在未发布 bootstrap/migration 准备态安装；全部 command 注册后由 framework bootstrap 运行私有 `_validate_mutation_policy_commands`，再将 Document 单向 seal。mod/downstream 不直接调用这项 authority。此后受保护 CRUD 必须同时处于 coordinator coverage 与当前业务 command authority 中，直接调底层 API 会在变化前 fail closed；history 使用 coordinator-owned replay coverage
- actor/source 权威冻结 — 受保护 delta 的 Entity actor log 及全部返回 Operation 必须等于 dispatch 开始时的 exact Actor/source；嵌套 helper 不能换人、换 source，callback 修改 Actor 会恢复并拒绝
- preflight 也是事务边界 — poll/governor/确认准备必须只读；它们返回 false、抛错或生成 pending 时，任何偷改都会在存储前拒绝并回滚。嵌套 dispatch 只能使用同一个 registry 和同一个 Document
- `CommandMutation` — 一笔命令需要主 Operation + 占位等派生 Operation 时合成一个 receipt；线程、manifest、状态广播等不可回滚动作放入 `DeferredAction`，只在提交与事件接受后执行
- W05 `notify.push` 外发边界 — producer 只返回一条 `external.status=pending` 的意图 Operation；限流扣额、EffectLedger、真正 provider 和 NotifyLog 都在同一个 `DeferredAction`。存储未提交或事件失败时四者调用数均为 0；action 失败只产生失败的 ActionReceipt，不改写已 committed 的业务 receipt
- W05 确认令牌事务边界 — token 先进入 `reserved` 而不是被 pop；只有 committed（含事件失败）才消费，完整回滚的 aborted 会释放重试，dirty/indeterminate 会冻结并返回 `confirmation_repair_required`。确认与取消都绑定创建 token 的 actor
- W05 asset 边界 — `entity.add/batch_add/update/hard_remove` producer 不再写 `meta.refs`；import、duplicate、undo、redo 与所有内置变更统一由 `AssetStore` 扫 canonical Document 多重集对账。这样 W04 回滚不会留下文件账漂移，无 Document 的 GC 会 fail-closed
- `broker` 和 `op_log` 参数暂时可以继续传，只为旧调用方的签名兼容，不会产生副作用
- 9 个内置 entity command（`entity.*` 命名空间）：
  - `entity.add` / `entity.batch_add` / `entity.update` / `entity.batch_update` / `entity.reorder`
  - `entity.delete` / `entity.restore` / `entity.hard_remove` / `entity.cascade_delete`
  - 每个产生 patches + reverse_patches，支撑 undo
  - 它们全部保持 `mutation_authority=False`：可作为未受保护类型的直接命令，也可在受保护类型的显式业务根中作为 nested helper，但不能写进 policy 充当权限根
  - `entity.hard_remove` 默认 `sources_allowed=("user", "system")`，拒绝 AI / sync 直调
- 2 个文档命令 `document.rename` / `document.set_acl`，以及外发意图 `notify.push`
- `register_builtin_commands(registry)` 一键注册全部 12 个内置命令

**⏳ 待补**（真有跨叶子依赖）

- `conflict_strategy` 真合并逻辑（CRDT / 3-way merge）— 等 [02-conflict-resolution/](../../02-data-flow/02-conflict-resolution/)
- `scheduling` 调度内核（queued / low-priority）— 等 [02-data-flow/](../../02-data-flow/)
- `acl` 真校验（role / 白名单解析）— 等 [03-authentication/](../../04-user-facing/03-authentication/)
- stage / compose 命令落地 — 设计已就位（kind 字段），待按需扩展
- 跨 skill 调用 — 等 [04-cross-skill-call/](../04-cross-skill-call/)

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态)。

## 怎么用

```python
from command import CommandRegistry
from builtin_commands import register_builtin_commands
from entity import Actor
from document import Document

# 1. 启动: registry + 内置 + mod 注册
registry = CommandRegistry()
register_builtin_commands(registry)
# mod_module.register(registry)        # mod 注册自己的 command

doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
actor = Actor(user_id="u1")

# 2. 生产增删改：coordinator 先开外层提交/coverage session，producer 再 dispatch。
def commit(command_id, args):
    return commit_coordinator.execute(
        lambda: registry.dispatch(
            command_id, args, actor=actor, source="user", doc=doc,
        )
    )

execution = commit(
    "entity.add", {"type": "obj", "data": {"name": "样例"}, "id": "e1"},
)
execution = commit(
    "entity.update",
    {"type": "obj", "id": "e1", "patches": {"name": "改名"},
     "expected_version": doc.get("obj", "e1").version},
)
execution = commit(
    "entity.delete",
    {"type": "obj", "id": "e1",
     "expected_version": doc.get("obj", "e1").version},
)

# execution.result 是 Operation / MutationBatch；receipt 决定是否真正 committed。
# 不要手工 op_log.append(...)、save(...) 或 broker.publish_change(...)。
```

## mod 端写自己的 command（推荐路径）

```python
# 02-mod/05-code/commands_register.py
from command import CommandMutation, CommandRegistry
from operation import new_operation

def register(registry: CommandRegistry) -> None:

    @registry.command(
        id="entry.create-with-template",
        label="基于模板新建条目",
        kind="native",
        touches=["entry"],
        mutation_authority=True,
    )
    def create_with_template(args, actor, source, doc):
        # generic helper 负责 create envelope、canonical position 与精确 reverse。
        helper = registry.dispatch(
            "entity.add",
            {"type": "entry", "data": load_template(args["template_id"])},
            actor=actor,
            source=source,
            doc=doc,
        )
        root = new_operation(
            command_id="entry.create-with-template",
            source=source,
            actor=actor.to_dict(),
            patches=(),
            reverse_patches=(),
        )
        return CommandMutation((root, helper))
```

如果 policy 真的引用这个 create root，它还必须同时列入 `lifecycle.create`、非空 name 所需的 `rename`，以及模板初始 data 每个字段的 allowlist。不要手写缺 `position` 的 create/delete Patch。

若业务根想复用 `entity.update`，不要复制 CRUD，也不要把 helper 升成权限根；在同一个 handler 中嵌套 dispatch，并把 root + helper 原样返回：

```python
from command import CommandMutation

@registry.command(
    id="entry.update-title",
    label="修改标题",
    mutation_authority=True,
)
def update_title(args, actor, source, doc):
    entity = doc.get("entry", args["id"])
    helper = registry.dispatch(
        "entity.update",
        {
            "type": "entry",
            "id": entity.id,
            "patches": {"title": args["title"]},
            "expected_version": entity.version,
        },
        actor=actor,
        source=source,
        doc=doc,
    )
    root = new_operation(
        command_id="entry.update-title",
        source=source,
        actor=actor.to_dict(),
        patches=(),
        reverse_patches=(),
    )
    return CommandMutation((root, helper))

# framework + mod commands 全注册后、开放请求前：
doc._validate_mutation_policy_commands(registry)  # framework bootstrap only；成功后单向 seal
```

policy 对 create 做组合授权：业务根除了要在 `lifecycle.create`，新 Entity 名称非空时还要在 `lifecycle.rename`，初始 data 的每个字段也都要在对应 field 列表。dot-path 按顶层根字段授权，但 helper 的 Patch/provenance 仍使用原始路径。只修改未声明 policy 的类型时，即使 registry 里存在其他受保护类型，也保持旧命令语义。

## 强制纪律

mod 业务代码**不允许**：

```python
# ❌ AP-C1 反模式
def my_handler():
    doc.add(Entity(...))           # 没 Operation, 没 OpLog, 没 undo
    entity.update(...)             # 同上
```

必须：

```python
# ✅ 正确做法
def my_handler():
    return commit_coordinator.execute(
        lambda: registry.dispatch(
            "entry.create", {...}, actor=..., source=..., doc=doc,
        )
    )
```

## Command 与 Commit 的边界

CommandRegistry 回答“这个意图现在能不能执行，单个 handler 是否完整结束，产生了哪些 Operation，以及成功提交后才允许做什么动作”。它的内层 mutation session 只保证 handler 失败不留下半次修改。CommitCoordinator 必须在 dispatch **之前**包住整个 producer，回答“整个 producer（可能含多次 dispatch）是否完整、文档和操作日志是否一起持久化、事件是否可发布、after-commit action 是否执行，这次调用能否安全重试”。受保护 CRUD 也从这个外层获得不可缺少的 coverage，因此不能先裸 dispatch、再把已经生成的 Operation 补交给 coordinator。

这个分工解决了旧结构中的一个根本问题：dispatch 曾经先 append OpLog、再由调用方保存文档；中间任一步失败都会留下“日志说成功、文档却没成功”或重复广播。现在 CommandRegistry 不再拥有任何提交副作用，事务顺序只有一个主人。

handler、poll、governor、确认准备或 coverage 校验失败时，dispatch **抛异常**，内层 mutation session 原位回滚；它不把异常吞成 `Operation.error` 再假装返回一次成功 mutation。生产外层 CommitCoordinator 再负责整笔 producer 与 store 失败的回滚。canonical OpLog 只记录实际提交的业务/rollback Operation；失败尝试若产品需要审计，应进入与成功 hash-chain 分开的 failure-attempt audit，不得伪造一条成功 Operation。

policy 生命周期 no-op 也不绕安全门：已删除实体再次 delete、已恢复实体再次 restore，仍先做 CAS 与 read-only 检查；合法 no-op 返回空 patch、Entity version 不变。普通 update 写入相同值仍可能是一次真实 CAS mutation 与 actor-log 事实，不能一概当作 no-op 丢掉。

`notify.push` 是这条边界的标准外发示例。Operation 只证明“系统接受了发送意图”，因此只记渠道端点和 `pending`，不写消息正文、幂等 key、凭证，也不伪造 message id 或耗时。真实发送结果由 NotifyLog / EffectLedger 留痕，不反向篡改已经耐久的 Operation。

W05 的 `DeferredAction` 仍是进程内对象：如果进程恰好在业务提交后、action 执行前崩溃，意图可能保留为 pending 而发送丢失。W06 必须把 action 意图、幂等键、重试状态和结果收进耐久 action journal / transactional outbox，再能承诺跨进程恢复。

确认执行的生产入口是 `sdk.persistence.execute_confirmation`。它完成
`reserve → CommitCoordinator.execute → finalize(receipt)`；直接在 producer 开始前
`take/pop(token)` 属禁止写法。对外 pending 不返冻结 args，避免 secret 进响应/幂等账本。
网页首段会获得一次性 approval nonce；MCP/AI 不获得 nonce、不广告 `ze_confirm`，
同一 actor 也不能自我批准。当前 PendingStore 有 TTL/容量上限，但仍是单进程内存状态；
W06 必须将 pending/reservation/frozen/repair 落入事务库。

Asset 采用同一条原则，但生命周期不是为每条命令创建一组 after-commit 增减动作：那仍会漏掉 import、duplicate 与 history 重放。`meta.refs` 现在只是从完整 Document 重建的观测，GC 必须携带 canonical Document 并先全量 reconcile。producer 唯一允许的 asset prepare 是 `put(bytes)`；它写入不可变、内容寻址的 blob，提交失败最多留下可安全回收的 orphan。`put_file`、promote、legacy refs、reconcile 与 GC 都受执行阶段 guard 保护；多进程锁和 orphan TTL sweeper 属 W06。

## 跑测试

```bash
cd 01-core/03-commands-operations/01-command/
python -m pytest test_command.py -v
```
