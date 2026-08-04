# 02-operation — Operation

> 一句话：**数据变更日志单元**。每次成功变更产生一个 Operation，含 patches + reverse_patches + source + actor + ts；落 OpLog（hash chain 防篡改）支撑 undo / 协作同步 / 审计。

## 在框架里的位置

- 上一级：[03-commands-operations/](../) — 指令与操作 5 个叶子
- 同级：[01-command/](../01-command/) / [03-pipeline/](../03-pipeline/) / [04-cross-skill-call/](../04-cross-skill-call/) / [05-ai-coworker/](../05-ai-coworker/)
- 上游：[Command](../01-command/) — 产生 Operation 的策略层

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览）|
| [SPEC.md](./SPEC.md) | 11 节完整规则（Patch / Operation / source 四态 / OpLog hash chain / undo 基础 / 持久化 / 红线）|
| [operation.py](./operation.py) | Operation / new_operation / OpLog 实现，并从中立协议叶子兼容 re-export Patch |
| [mutation_patch.py](../../_protocol/mutation_patch.py) | Patch 的唯一实现与所有权位置，供 Document replay 与 Operation log 单向共用 |
| [test_operation.py](./test_operation.py) | pytest 单元、持久化与崩溃尾恢复回归 |

## 当前状态

**✅ 已实装**（全部无跨叶子依赖项）

- `Patch` 值类型（frozen dataclass）：entity_type / entity_id / field / op / before / after；3.36 追加可选 `provenance` / `position` / `ancestry`
- `Operation` 值类型（frozen dataclass）：id / command_id / source / actor / ts / patches / reverse_patches / group_id / trace_id / kind / error / document_id / session_id / external / pipeline
- Pipeline carrier：可选顶层 `pipeline` 只接受 `ze.pipeline-start.v1` / `ze.pipeline-control.v1`，缺省时完全省略；构造、编码、解码均深拥有并执行独立资源预算。本版只提供耐久载体，不签发运行权限
- Pipeline authority Batch 3–4：core 3.40 只允许 private reserved-ID factory 产生 sealed v2 Operation；seal 同时绑定签发进程与精确 Document 对象，即使另一个 Document 有相同 id 也不能沿用。底层 durable codec 已能写 v2，但 generic `Operation/new_operation`、复制、pickle、replace、fork 继承与相似对象全部不能获得 seal。CommitCoordinator 必须把自己的精确 Document 交给 preflight，落盘前和 committed+durable 后各做一次完整历史证明；结果只是绑定 exact store/Document 的内部 attestation，不是 worker grant。v1 永久只作为 carrier
- Pipeline dormant delegated check：core 3.40 可用上述 attestation 校验一次精确的 worker 请求；每次 prepare/consume 都会重验 canonical HistoryAuthority 对象图、fresh OpLog head、完整归约、原业务 command policy、item→entity、stage、字段/lifecycle、实际 worker 身份、entity version 与 cardinality。请求一次性消费且 `execution_authority=False`；它不 dispatch、不改 Document、不写 manifest、不启动 worker，dynamic create 也继续拒绝到 Batch 5
- `new_operation(...)` 工厂：校验 source ∈ SOURCES + kind ∈ KINDS；自动填 id / ts / trace_id
- `OpLog` 内存版：append + hash chain（sha256，sort_keys 字段顺序不变性）
- `OpLog(path)` 持久化版：NDJSON 追加、文件 `fsync`、跨进程 `flock` 验链续写；LF 是完整 record 标记
- 截断尾恢复：锁内验证所有完整 record，只物理截掉最后一个未换行片段并 `fsync`；续写后再次重启仍能读到新 op
- 存储边界：最终文件与直接父目录拒绝 symlink，日志必须是单硬链接 regular file；新建/既有日志都收紧为 `0600`，FIFO 等特殊文件不会阻塞进程
- 输入边界：完整 record 严格拒绝重复 JSON key、NaN/Infinity、超深/超长/超总量数据；失败只返回稳定错误码，不回显路径或日志正文
- `OpLog.verify_chain()`：任何篡改（op body / self_hash / prev_hash）都能检出
- `OpLog.by_entity(type, id)` / `by_source(source)` 过滤查询
- 常量导出：`SOURCES` / `KINDS` / `OPS` / `ENTITY_META_FIELD` / `ZERO_HASH`

**⏳ 待补**（真有跨叶子依赖）

- OpLog 分片、压缩与历史索引 — 当前单文件到硬上限后 fail closed，后续以显式归档协议扩展
- 协作同步（op 推到远端）— 等 [01-realtime-sync/](../../02-data-flow/01-realtime-sync/)
- 跨 skill trace_id 串联 — 等 [04-cross-skill-call/](../04-cross-skill-call/)

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态)。

## 怎么用

```python
from operation import Patch, new_operation, OpLog
from entity import Actor

# 1. Command.execute() 内部构造 patches + Operation
patches = [Patch("obj", "e1", "name", "set", before="old", after="new")]
reverse_patches = [Patch("obj", "e1", "name", "set", before="new", after="old")]

op = new_operation(
    command_id="object.update",
    source="user",
    actor=Actor(user_id="u1").to_dict(),
    patches=patches,
    reverse_patches=reverse_patches,
)

# 2. 这段只演示值类型与 hash-chain；它没有同时修改 Document，不能冒充生产写入。
# 生产路径必须在同一个 CommitCoordinator producer 内修改 Document 并返回对应 Operation。
# OpLog.append 只在存储叶子的独立测试/工具中直接调用；Command.dispatch 不会自动 append。
log = OpLog()
entry = log.append(op)  # 非生产拼接示例

# 3. 审计 / 查询
log.by_entity("obj", "e1")    # 这个 entity 的全部变更
log.by_source("ai")           # AI 干过哪些事
log.verify_chain()            # 检查是否被篡改

# 4. undo/redo 已由 history-rollback 的 UndoController 实装；生产入口走
# prepare_undo/redo → CommitCoordinator → confirm_deferred，不手工 apply/append。
```

## 设计要点

- **Patch 字段级**：粒度小，undo / 协作 sync / 字段级 merge 都好做
- **missing 不等于 null**：从缺失新增字段时，reverse 必须是 `delete`；显式写 null 仍是 `set`。多 patch 的 reverse 还要把整体顺序倒过来
- **可逆信封只在需要时出现**：`ancestry` 还原 dot-path 的 missing/null/scalar 父链，`provenance` 还原原始字段路径的 AI 来源，`position` 还原 canonical collection 顺序
- **旧 hash 保持可验证**：三个新成员为 `None` 时 `Patch.to_dict()` 完全省略它们，旧 Operation body/self_hash 不会因升级被重写；一旦出现则属于被 hash 的审计正文
- **Pipeline 权限不塞进 external**：`external` 记录外部效果来源，`pipeline` 记录业务 Operation 自身批准的运行描述符；两者职责不同。`pipeline=None` 时旧 bytes/hash 逐字不变，存在时描述符属于 hash 正文
- **载体、seal、attestation、dormant request、grant 各负一责**：3.37 的 v1 只有资源受限载体，永久不能追认为权限；3.38 的 strict v2 codec/reducer 只会解释完整事实；3.39–3.40 的 private factory 才能签发绑定原始 Document 和进程的 exact-object seal，CommitCoordinator 再把 prospective 与 durable fresh history 逐字对上后保存 process-local attestation。attestation 的 keyed integrity 绑定 active store/run key、精确 Document 和整份归约，每次私有消费都会重算；静态 Start/policy 或动态 generation/state/head 被改动时会同时撤掉 active 与 identity ledger。3.40 的 dormant request 只证明一次具体范围在当下仍合法，不进入 Operation/receipt/event/SDK，也不能启动或恢复 worker；Batch 5 的 grant issuer/registry 仍需基于 fresh facts 重新签发短期执行资格。因此公开 Operation API 始终只能表达和查询历史，没有执行能力
- **耐久正文不等于公开响应**：command、history、oplog introspection、HTTP/MCP/CLI 共用唯一 projector 移除完整 `pipeline` 描述符；Operation 直接生成无权限正文的 fast projection，OpLog 先 filter/limit 再逐字段复制，因此不会为了丢弃 4 MiB Start 而先复制整段私密历史。被删减的 history entry 会标记 `canonical_body_redacted=true` 与 `redacted_fields=["op.pipeline"]`。保留的 `self_hash` 对应服务端完整 canonical 正文，客户端不能用公开删减版复算；`verified` 只表示服务端已对完整链验真。UI 只接有界状态摘要，避免大 WorkList 按整段历史放大或泄露执行输入
- **Operation frozen**：要"改"就造新 op，永不 mutate
- **hash chain 防篡改**：每条 entry 的 self_hash 算上 prev_hash + sorted JSON；任何修改都断链
- **源数据保护**：`entries()` / `by_*()` 都返回 deepcopy，caller mutate 不影响内部
- **不区分 source**：所有 source 走同一份数据结构，undo 对 AI / sync / system op 一视同仁
- **失败不伪装成成功 Operation**：handler/preflight/coverage 失败应抛出并回滚；canonical OpLog 只收 committed mutation。失败尝试若需留痕，使用独立 failure-attempt audit

## 跑测试

```bash
cd 01-core/03-commands-operations/02-operation/
python -m pytest test_operation.py -v
```
