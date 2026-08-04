# Operation — 数据变更日志单元

## 1. 这是什么

每次成功的数据变更（增 entity / 改字段 / 软删 / 硬删 / 级联删 / mod 自定义动作）都产生**一个或一组有序 Operation**——记录"谁、在何时、通过哪个 Command、对哪些 entity 做了什么、怎么撤回"；复合业务根可用第一条空 patch Operation 表达意图身份，后续 helper Operation 承载真实细节，但整组必须由同一提交原样落下。

Operation 是 ZE 框架的**审计 + undo + 协作同步**三件套的最底层数据。

业务白话视角：业务同事在 ZE-knowledge-base 改了一条知识的标题，框架内部产生一个 Operation：`{command_id: "entry.update", source: "user", patches: [字段值变化], reverse_patches: [反向变化], actor: 张三, ts: ...}`。这条 Operation 落进 OpLog（hash chain 防篡改），后续支撑：
- **undo**：从 OpLog 取最近一条，把 `reverse_patches` 应用回去
- **协作同步**：把 Operation 推给其他端，他们 apply patches 跟上
- **审计**：何时谁改了啥，OpLog 全程可查

## 2. Patch — 字段级原子变更

```python
@dataclass(frozen=True)
class Patch:
    entity_type: str        # 哪个 entity_type
    entity_id: str          # 哪个 entity id
    field: str              # 哪个字段; "__entity__" 表示元操作 (create / delete / restore)
    op: str                 # set / delete / create / soft_delete / restore / name
    before: Any = None      # 变更前
    after: Any = None       # 变更后
    provenance: dict | None = None  # 可选:原始 field path 的 AI 来源前后端点
    position: int | None = None     # 可选:结构 create/delete 的 canonical bucket 位置
    ancestry: dict | None = None    # 可选:dot-path 顶层父链前后端点
```

**op 六态**：

| op | 含义 | field 用法 | before / after |
|---|---|---|---|
| `set` | 设置字段值（含新增 + 修改）| 字段名或 dot-path | before=旧值、after=新值；从 missing 新增时 reverse 必须用 `delete`，不能只看 `before=None` |
| `delete` | 删字段（从 data 移除）| 字段名或 dot-path | before=旧值、after=None；policy opt-in history 真移除，legacy 无 policy 保留旧 set-to-null 兼容 |
| `create` | 创建 entity | `"__entity__"` | before=None、after=entity.to_dict() |
| `soft_delete` | 软删 entity | `"__entity__"` | before=undeleted snapshot、after=deleted snapshot |
| `restore` | 恢复软删 | `"__entity__"` | before=deleted snapshot、after=undeleted snapshot |
| `name` | 改 entity.name | `"__entity__"` | before=旧 name、after=新 name |

`before=None` 单独不能区分“字段不存在”和“字段存在且值为 JSON null”。存在性由操作程序表达：

- missing → value：forward `set(before=None, after=value)`，reverse `delete(before=value, after=None)`；
- null → value：forward `set(before=None, after=value)`，reverse 仍是 `set(before=value, after=None)`；
- value → missing：forward `delete(before=value, after=None)`，reverse `set(before=None, after=value)`。

三个可选信封只在基础六字段不够精确时使用：

- `ancestry` 精确为 `{"field": root, "before": {"present": bool, "value": ...}, "after": {...}}`。当 dot-path 的父链原来是 missing、null 或 scalar 时，重放用它恢复整个顶层 root；reverse 交换 before/after。
- `provenance` 使用同样的 before/after presence 端点，但 `field` 必须是实际 patch 的原始路径（如 `box.a`），不是 policy 查权时的顶层 `box`；reverse 交换端点。受保护 AI provenance 必须一对一绑定本次 patch path。
- `position` 是 exact 非负 int，仅用于 Entity `__entity__` create/delete。受保护结构变更必须携带它；forward/reverse 保持同一位置，使 hard-remove undo 能插回 canonical 段原位，read-only overlay 仍在 canonical 段之后。字段、文档 metadata 与非结构生命周期 patch 不得携带 position。

`Patch.to_dict()` 在三个值为 `None` 时完全省略对应成员，保证 3.35 及更早 Operation 的 JSON body 与 OpLog hash 字节不变；成员出现时则是审计正文的一部分，会参与 self_hash。构造输入与 property/to_dict 返回值都深层脱离，caller 不能借别名修改已验证 Operation。

**保留 entity_type 哨兵 `__document__`（DOCUMENT_TARGET）**:
文档级元数据变更(当前仅 `document.rename` 改 doc.name)也走 Patch/Operation/OpLog/广播标准通道,
用 `entity_type="__document__"`、`entity_id=doc.id`、`field="name"`、`op="set"`。这样**单文档的 doc-meta
变更不必另开 side-channel**,保持"一切变更皆 Operation"不变。`affected_entities` 见到此哨兵时 snapshot 取
`doc.meta_dict()`;前端按 `entity_type==="__document__"` 把 snapshot 路由到 meta 而非 entities。

**为什么字段级**：
- undo 颗粒度小 → 用户体验好（只撤这一字段，不影响别的）
- 协作 sync 时 patches 体积小 → 网络省
- 冲突解决能字段级判断（A 改字段 X、B 改字段 Y → 可以 merge）

## 3. Operation — 完整变更记录

```python
@dataclass(frozen=True)
class Operation:
    id: str                              # uuid4 hex
    command_id: str                      # "entity.update" / mod-defined
    source: str                          # user | ai | sync | system
    actor: dict                          # Actor.to_dict()
    ts: float                            # 时间戳
    patches: tuple[Patch, ...]           # 正向变更
    reverse_patches: tuple[Patch, ...]   # 反向 (undo 用)
    group_id: str = ""                   # 连续同 group_id = 1 个 undo 单元
    trace_id: str = ""                   # 跨服务/跨 skill 追踪 ID
    kind: str = "native"                 # native / stage / compose / rollback
    error: dict | None = None            # 失败时 error_contract dict
    document_id: str = ""                # 可选 realtime/checkpoint 文档身份；空值序列化时省略
    session_id: str = ""                 # 可选会话身份；空值序列化时省略
    external: dict | None = None          # 可选外部效果意图/provenance；None 时整键省略
    pipeline: dict | None = None          # 可选 Pipeline Start/Control 描述符；None 时整键省略
```

**reverse_patches 怎么构造**：它是 forward patch program 的真正逆程序，不是机械逐行交换。先为每个 patch 构造逆项：create ↔ delete、soft_delete ↔ restore、普通 set 仍是 set；从 missing 建字段则 set → delete；provenance/ancestry 交换端点；结构 position 保持不变。然后把**整段顺序倒过来**。例如 forward 连续 `x: 1→2`、`x: 2→3`，reverse 必须先 `3→2` 再 `2→1`；批量 create 也必须倒序 delete。

**group_id 用法**：拖拽控件可能产生 100 个 op（每帧 1 op），用同一 group_id 标记 → undo 时一次性撤 100 个 → 用户感知为"撤回拖拽动作"一次。

**trace_id**：跨服务调用链追踪。比如 mod 端 command 又调了别的 skill 的 command，trace_id 串起来。

**kind 四态**：
- `native` — 普通业务 command
- `stage` — 长任务的某一阶段（AI pipeline 中间步骤）
- `compose` — 把多个 native command 组合起来的复合命令
- `rollback` — undo 产生的反向 op 本身（用于"undo 的 undo = redo"识别）

**error**：数据结构保留这个可选诊断信封，但它不是“catch handler 异常并提交”的许可。正常 committed 业务/rollback Operation 应为 `None`；handler、preflight、policy/coverage 或 store 前失败必须抛出并由 mutation session 回滚，canonical OpLog 不追加伪成功记录。若产品需要追踪失败尝试，应使用与成功 hash-chain 分离的 failure-attempt audit；提交后的外部 action 失败则写 ActionReceipt/专属效果日志，不反向篡改 Operation。

**pipeline**：这是 Pipeline 权限事实的耐久载体，不是外部效果 provenance，因此不能塞进 `external`。3.37.0 只建立 carrier：存在时顶层 `schema` 必须是 `ze.pipeline-start.v1` 或 `ze.pipeline-control.v1`，未知版本 fail closed；构造输入、公开 getter、pickle 与 `to_dict()` 均不共享 caller 可变对象。Start canonical JSON 上限 4 MiB、深度 32、节点 100,000、单字符串 256 KiB；Control 分别为 64 KiB、12、2,048、16 KiB。writer 在整块 Pipeline JSON 编码前先取得有界 owned snapshot 并按 legacy spacing 精确累计预算；durable reader 仍先受整个 OpLog record 的 16 MiB 兼容上限约束，逐行 decode 后立即按相同 profile 复验，拒绝项不会进入已加载历史。`pipeline=None` 时键完全省略，旧 Operation bytes、self_hash 和 14 参数 pickle 形状保持不变。

本载体本身**绝不授予执行权限**。3.37 阶段尚未实现 Start/Control 字段语义、sealed factory、fresh OpLog reducer、CommitCoordinator durable hook 或进程内 grant；当时带 `pipeline` 的 Operation 只是一段被 hash 的已知版本 metadata。

3.38.0 增加 private、只读的严格 `ze.pipeline-start.v2 / ze.pipeline-control.v2` codec 与 complete-history reducer，但**没有修改上段 writer 白名单**。v2 Start 继续使用 4 MiB/32 层/100,000 节点；v2 Control 为使最多 4,096 个合法、最坏 256-byte 且需 JSON escape 的 item id 真能装入合同，预算调整为 **2,179,072 bytes/12 层/4,123 节点**，单字符串仍为 16 KiB。原因是 v1 在 seal 出现前已经可由普通 caller 写入，不能事后追认；v2 必须等 sealed、可预留 Operation id 的 factory、writer activation、mandatory pre-store hook 与 committed+durable 后 fresh reconcile 在 Batch 3 同一批原子接线后才允许落盘和消费。reconcile 只可产出内部 sealed attestation，不能把公开 reducer projection 当成 grant；grant issuer/registry 仍按 Batch 5 接线。3.38 可以校验/归约显式提供的 v2 fixture，却不能通过普通 `Operation/new_operation` 创建 v2 历史，也不会签发任何运行权限。

3.39.0 原子完成上述 Batch 3：底层 record codec 为 durable reader/writer 启用 v2 资源 profile，但公开 `Operation/new_operation` 继续拒绝 v2。private factory 先预留 32 位小写十六进制 id，再从 active registered-command root 一次性取得 actor/source/document、registration digest 与精确 Document policy，构造后立刻把 exact Operation object 和 canonical body hash 放入 process-local seal ledger。复制、deepcopy、pickle、replace、fork 继承或字段/body 漂移都不能保留 seal。CommitCoordinator 在 store 前要求本次每个 v2 Operation 都有 seal，并以 fresh 旧历史 + exact 待提交 tuple 做 prospective full reduction；提交后只在 Document 与 OpLog 均 durable、history transaction 已最终收口时，从 exact `FileCommitStore` 的 exact `OpLog` 重新读取完整历史，要求 count/tail 与 prospective proof 一致并重跑 reducer。成功结果仅保存在 private per-store/run attestation registry；它不可复制/序列化、fork 后失效，也不进入 receipt、event 或 SDK。任何 post-durable 不可验证都返回 typed replay-unsafe error；不得回滚已耐久 Document、不得重放业务命令、不得签 attestation。grant、worker 消费和 cancel 撤权仍未在本叶子完成。

完整描述符也不属于 HTTP/MCP/CLI 的公开 Operation 响应或 history/oplog reader。唯一 private public-projector 会在 command、history、introspection、MCP resource/tool 与 CLI 共同路径删除 `pipeline`；Operation fast seam 不构造该字段，OpLog 则在锁内先 filter/limit、再逐字段复制并跳过它，避免 4 MiB Start 按整段历史放大内存/响应或泄露长期 WorkList/执行输入。浏览器只消费后续有界的 Pipeline status/authority summary。被删减的 history entry 必须显式带 `canonical_body_redacted=true` 和 `redacted_fields=["op.pipeline"]`：其 `prev_hash/self_hash` 仍绑定未删减 canonical 正文，不能对公开投影重新计算。reader 返回的 `verified` 是服务端对完整 canonical 链的验证结果，不是客户端对投影的可复算声明。canonical OpLog 与内部审计仍保留完整正文。

## 4. source 四态

Operation.source ∈ `{"user", "ai", "sync", "system"}`：

- **user** — 真人手动触发（点击 / 拖拽 / 快捷键）
- **ai** — AI pipeline 产生（generation / extraction / auto-layout）
- **sync** — 远端协作者通过同步通道发来的 op
- **system** — 自动化任务（定时备份 / 数据迁移 / 自动清理）

**所有 source 产生的 Operation 走同一份数据结构**——undo 系统不特殊对待 source。用户能 undo AI 产生的 op（"撤回 AI 生成"），也能 undo system op（管理员撤回自动任务）。

按 source 过滤 OpLog → 得审计视图（"AI 在最近 1 小时做了哪些事"）。

对 mutation-policy 受保护的实际变化，Operation 身份不是 handler 可随意填写的文字：root 与所有 helper Operation 的 `source` 必须逐字等于 dispatch 开始时冻结的 dispatch source，`actor` 必须逐字等于同一时点冻结的 Actor；第一条业务 root 的 `command_id` 必须等于授权的最外层 command id。**这两条是独立权威轴**：Actor 自身的 `source` 描述主体来源，Operation/dispatch source 描述本次动作通道，框架不要求二者互相相等（例如合法 worker 可为 `Actor.source=ai`、dispatch/Operation source=`system`）。任一轴分别漂移都会在 store 前回滚。未受保护类型继续使用既有 Operation 兼容语义。

## 5. OpLog — append-only 日志 + hash chain

```python
class OpLog:
    """Append-only 日志, 每条带 prev_hash + self_hash 链, 任何篡改都能检测."""

    def append(op: Operation) -> dict           # 返回 entry {op, prev_hash, self_hash}
    def verify_chain() -> bool                   # 全链校验
    def entries() -> list[dict]                  # 全部 entries (copy)
    def by_entity(type, id) -> list[dict]        # 影响某 entity 的 ops
    def by_source(source) -> list[dict]          # 某 source 的 ops
```

**hash chain 原理**：每条 entry = `{op, prev_hash, self_hash}`；`self_hash = sha256(prev_hash + json(op))`。任何一条 op 改了，自己的 self_hash 错；后续每条的 prev_hash 跟不上 → 整条链断 → `verify_chain()` 返 False。

**链头**：第一条 entry 的 prev_hash = `"0" * 64`（约定）。

**append 是唯一写入路径**：不允许直接修改 entries / hash。任何"我要插入历史 op" 的需求都该走 append（用 ts 排序还原历史顺序）。

`OpLog()` 是纯内存模式；`OpLog(path)` 是单文件 NDJSON 持久化模式。二者共享同一条 hash-chain 语义，调用方不能因存储形态不同而绕过 `append()`。

## 6. Python API

```python
from operation import Patch, Operation, new_operation, OpLog

# Patch (frozen dataclass)
p = Patch(entity_type="obj", entity_id="e1", field="name", op="set",
          before="old", after="new",
          provenance=None, position=None, ancestry=None)  # 三项均可省略
p.to_dict()                                          # ✅

# Operation 通过 new_operation 工厂构造 (统一填默认值)
op = new_operation(
    command_id="entity.update",
    source="user",                                   # 必须是 SOURCES 之一
    actor={"user_id": "u1", "source": "user"},      # Actor.to_dict()
    patches=[p],
    reverse_patches=[Patch(..., before="new", after="old")],
    group_id="",                                     # 可选
    trace_id="",                                     # 默认 uuid4
    kind="native",                                   # 默认 native
)

# Operation 字段都是只读 (frozen)
op.id / op.command_id / op.source / op.actor / op.ts
op.patches / op.reverse_patches
op.group_id / op.trace_id / op.kind / op.error
op.pipeline                                           # None 或 detached descriptor copy
op.to_dict()                                         # ✅ 含 patches 展开

# OpLog
log = OpLog()
entry = log.append(op)                               # ✅ 返回 {op, prev_hash, self_hash}
log.verify_chain()                                   # ✅ bool
log.entries()                                        # ✅ list copy
log.by_entity("obj", "e1")                           # ✅ 影响该 entity 的 entries
log.by_source("user")                                # ✅ 某 source 的 entries
```

## 7. undo / redo 基础

本叶子只拥有 Operation + OpLog 数据结构；undo / redo 控制器已经在 [04-history-rollback](../../02-data-flow/04-history-rollback/) 叶子实装，本叶子不复制其游标和提交流程。

但 undo 的根基在这里：

```python
# 生产调用形状（概念示意）: prepare 必须发生在 coordinator producer 内。
from sdk import MutationBatch

prepared = {}
def produce_undo():
    record = undo_controller.prepare_undo(actor=actor)
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
        undo_controller.confirm_deferred(record)    # 提交后才推进 redo 游标
    else:
        undo_controller.discard_deferred(record)    # Document 回滚归 coordinator
```

**关键**：undo 也产生 Operation（kind="rollback"），落 OpLog → undo 也可被 redo（再 undo 这个 rollback）。

## 8. 持久化

`path=None` 时仅保留进程内历史；传入 `path` 时，每条 entry 以一行 NDJSON 追加。持久化 append 必须满足：

1. 进程内写入由同一实例锁串行，文件写入在 `flock(LOCK_EX)` 内完成；
2. 只有完整 LF 结尾的 record 才算提交边界；写入 `JSON + LF` 后必须 flush + file `fsync`，成功后才更新内存并返回；
3. 文件由其他实例改变时，append 必须在同一文件锁内重读并验证完整 hash chain，再从真实 tail 续写；普通单写者可用 dev/inode/size/mtime/ctime 全一致的缓存快路，不能只凭内存 tail；
4. 启动或 append 若发现最后一个 LF 之后还有任意 bytes，视为未提交尾片：先验证所有 LF-terminated record，再物理 truncate 到最后一个 LF 并 file `fsync`。只有修复持久化成功后才可开放续写；
5. 已经 LF-terminated 的非法 UTF-8、非法 JSON、错误 entry 结构或断裂 hash chain 是已提交数据损坏，必须 `oplog_corrupt` fail closed，不能当截断尾静默丢弃；
6. 恢复后的第一次 append 以及后续第二、第三次新进程重载都必须看到同一条完整链，不能只让当前内存视图看起来成功。
7. 最终日志文件和直接父目录都必须以 `O_NOFOLLOW` 打开；日志只能是 `st_nlink == 1` 的 regular file。symlink、hardlink、目录、socket、device、FIFO 一律 `oplog_path_unsafe`，其中 FIFO 必须用 non-blocking open 防止启动挂死；新文件和既有文件都收紧为 owner-only `0600`。
8. 每条完整 record 必须按严格 UTF-8 JSON 读取：重复 object key、NaN/Infinity、溢出 float、超长整数、过深嵌套和非法 UTF-8 都是 `oplog_corrupt`；writer 使用同一接受域，不能写出 reader 日后拒绝的数据。
9. scanner 必须逐 record 有界读取，并在分配大对象前检查单 record 与总文件字节上限。超过上限时读取报 `oplog_limit_exceeded`；append 在写入新 record 和更新内存之前拒绝超长 record 或超总量文件。锁内已经发现的截断尾仍按第 4 条独立耐久修复，不能因新 entry 非法或超限而把修复回滚。
10. `oplog_path_unsafe`、`oplog_corrupt`、`oplog_limit_exceeded` 与 `oplog_entry_invalid` 都是可依赖的稳定错误码；异常 message、context 与 cause chain 不得包含绝对路径、actor/patch 正文或底层 JSON 解码异常持有的原始 record。

当前实现护栏是单 record 16 MiB、单文件 256 MiB、JSON 容器深度 128、整数最多 4300 位；这些是框架内部 fail-closed 边界，不是 mod 可关闭的运维旋钮。到达单文件上限必须先走未来的显式归档/分片协议，不能直接调大常量继续写。

当前只承诺单文件 NDJSON 与 file 内容耐久；容量上限是防资源耗尽边界，不是自动归档。尚无按日分片、压缩、历史索引，也没有把 Document 与 OpLog 合成一个原子事务。新文件目录项未由本模块单独给出 directory-fsync receipt，所以业务提交仍必须保留 FileCommitStore 的 `indeterminate` 语义。

路径威胁模型限于同 UID 的合作进程：实现固定直接父目录 fd 并拒绝最终路径/直接父目录 symlink，但不承诺抵御同一账号恶意进程同时替换更上层祖先目录。若部署边界包含不可信同 UID 代码，必须由外层把项目根放进独立权限域或容器，而不是把本地 OpLog 当作安全沙箱。

## 9. Mod 端必填 + 注册

mod 端**不直接构造 Operation**。Operation 由 Command 层产生（命令的 execute 函数内部调 `new_operation`）。详见 [01-command/SPEC.md §6](../01-command/SPEC.md)。

## 10. 红线规则 + 反模式

红线（违反破坏审计 / undo / 协作）：

- **Operation 是 frozen** — 不能 mutate；要"改"就造新 op
- **业务代码不能跳过提交协调器直接 apply patches** — history 模块可以在外层 mutation session 内私下重放，但随后必须由 CommitCoordinator 原子提交 rollback Operation；失败由同一外层恢复（AP-O1）
- **reverse_patches 必须真能 undo** — caller 责任：写错则 undo 出错（AP-O2）
- **不能伪造 prev_hash** — OpLog.append 内部计算，外部不传（AP-O3）
- **kind="rollback" 的 op 也是 op** — 不能从 OpLog 删除任何 entry（即使是 rollback 撤回的）

反模式：

- **AP-O1** 业务代码绕过 Command + OpLog 直接调 entity.update → 失审计、失 undo、协作失同步
- **AP-O2** reverse_patches 写错（before/after 没换 / op 没反转）→ undo 后状态错乱
- **AP-O3** caller 自己算 prev_hash 传 OpLog.append → 破坏 chain 一致性；append 内部应自取 tail
- **AP-O4** 把整个 entity dump 进 patches[0].after → 浪费、网络重 / 失去字段粒度合并能力
- **AP-O5** group_id 滥用（不相关的 op 用同 group_id）→ undo 一锅端，用户感知 "撤回怎么把别的也撤了"
- **AP-O6** catch 失败并造 `Operation.error` 追加到 canonical OpLog → 把“未发生”伪装成成功历史，也可能推进 hash-chain/undo 游标；应抛错回滚，并按产品需要写独立 failure-attempt audit

## 11. 当前实装状态

### ✅ 已实装

| 项 | 说明 |
|---|---|
| `Patch` 值类型 | frozen value：六个旧字段 + 可选 provenance / position / ancestry；输入与输出深层脱离，三项缺省时序列化完全省略以保持旧 hash |
| `Operation` 值类型 | frozen dataclass: id / command_id / source / actor / ts / patches / reverse_patches / group_id / trace_id / kind / error / document_id / session_id / external / pipeline + to_dict；无 pipeline 时旧 bytes/hash/pickle 兼容 |
| Pipeline carrier 与 sealed v2 | v1 仅是永久 carrier；strict v2 只能由 private reserved-ID factory 签 exact-object seal，CommitCoordinator pre-store/fresh reconcile 后只留下内部 attestation。公开 factory、reducer projection 或 attestation 均不是 worker grant |
| `new_operation(...)` 工厂 | 校验 source ∈ SOURCES；默认 id / ts / trace_id；patches → tuple |
| `OpLog` 内存/持久化双模式 | `path=None` 为内存链；`path` 为 flock + file-fsync 单文件 NDJSON，含截断尾物理恢复 |
| `OpLog.verify_chain()` | 全链校验，任何篡改返 False |
| `OpLog.entries()` | 返回 entries copy |
| `OpLog.by_entity(type, id)` | 过滤影响某 entity 的 entries |
| `OpLog.by_source(source)` | 过滤某 source 的 entries |
| ZeError 共享 | 从 `entity` 模块 import（跟 document / asset 一致）|
| history consumer | `UndoController` / atomic replay / deferred cursor 已在 `02-data-flow/04-history-rollback` 实装；Operation 叶子只拥有值与日志合同 |

### ⏳ 待补（真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| OpLog 分片 / 压缩 / 历史索引 | 容量、归档与查询策略 |
| 协作同步（把 op 推给远端）| `02-data-flow/01-realtime-sync` |
| 跨 skill trace_id 串联 | `03-commands-operations/04-cross-skill-call` |

### core 3.40 权限边界补充

- v2 seal 绑定 exact Operation、签发 PID 与签发时的 exact Document；同 id 的另一 Document、另一 runtime/store 不能沿用。CommitCoordinator preflight 必须传入自己的 exact Document，permit 绑定 exact store/OpLog/Document/Operation tuple。
- attestation 绑定 exact store/Document，process-secret keyed integrity 覆盖 active key 与整份 reduction；静态 Start/policy/item/target/stage 或动态 generation/state/terminal/reconcile/head/control 被篡改时，下一次私有消费会 fail closed 并清理 active/identity ledger。
- dormant delegated request 只做一次无副作用校验：重验 canonical HistoryAuthority exact object graph/fresh head、完整 reduction、原业务 policy、worker actor/source、item→entity、stage、字段/lifecycle、entity version 和 cardinality。它固定 `execution_authority=False`，不 dispatch、不产生 Operation、不改 Document、不写 manifest/action/Control、不注册或启动 worker；dynamic create 与 grant/lifecycle 等待 Batch 5。
- 公开 `Operation/new_operation` 与 reducer projection 始终只是数据/解释 API，不能签 seal、取得 attestation/request/grant 或执行 worker。
