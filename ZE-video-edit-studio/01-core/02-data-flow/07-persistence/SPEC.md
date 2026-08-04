# Persistence Commit SPEC

## 1. 目标与边界

本叶是提交控制面，不拥有业务 Command、Document 数据模型或实时传输。它拥有三件事：

1. 所有写入口共用的提交顺序；
2. 存储与事件之间唯一的端口；
3. 调用方可以审计的 `CommitReceipt` 与 canonical event payload。

状态和失败分类必须复用 `03-commands-operations/02-operation/commit_contracts.py`，不得在 HTTP、MCP 或前端再造另一套“保存成功”枚举。

历史 W06.1–core 3.29.1 在同一叶新增 SQLite transaction kernel、业务快照/legacy 影子证据、V5 repository proof、冻结的V6 atomic substrate、V7 local revision修复、历史V8–V11 production/runtime演进与当时current V12 install closure carrier。V12用4张私有表表达完整member mapping、baseline projection与mount前outer atomic receipt；3.27.0增加private install writer，3.28.0增加plan-free durable auditor，3.29.0增加phase-1 finalizer，3.29.1再把共享事实校验收成neutral verifier。3.30.0/V13追加四张direct-predecessor carrier、正反guard和fresh全字段语义重建；current 3.31.0又完成四阶段共享纯持久化mapper与第一个asset-provider fixed-SQL writer。该writer仍无production issuer/caller，audit、activation、mount writer、cutover coordinator、authority router、dispatcher和生产消费端也仍不存在；普通启动仍是legacy/empty，生产读写仍走`FileCommitStore`，也不包含离线或跨设备同步。

kernel receipt 与本页 2.3/2.4 的业务 `CommitReceipt` 是两个不同类型、不同事实域；adapter 在正式业务行尚未同事务落库前不得互相转换。

每个已打开 project 只能有一个共享 coordinator（或多个 coordinator 共享同一把 project commit lock）。各 surface 各建一把默认锁不构成并发控制。

## 2. 对外类型

### 2.1 MutationBatch

`MutationBatch.operations` 是非空、Operation id 唯一的 tuple。它表示一笔逻辑提交包含多条 Operation，不表示多笔可以部分成功的提交。一个 batch 只生成一个 commit id、一个 receipt，receipt 的 `operation_ids` 保持输入顺序。

### 2.2 CommitIntent

CommitIntent 包含 commit id、Document、Operation tuple、trace id 和生效 SavePolicy。它由 coordinator 在 producer 返回后创建；surface 无权自行伪造提交结果。

### 2.3 CommitStore

唯一方法：

```python
commit(intent: CommitIntent) -> CommitReceipt
```

一次调用代表一次逻辑提交。开始尝试写入后，store 应返回事实完整的 receipt，不应让调用方依据异常文字猜“可能成功”。第三方 store 若违反合同并抛异常，coordinator 只能保守报告 Document/OpLog 均为 `unknown` 的 `indeterminate`，并禁止广播。

### 2.4 CommitExecution

| producer 结果 | receipt | events | actions |
|---|---|---|---|
| `None` | 无 | 空 | 空 |
| `PendingConfirmation` | 无 | 空 | 空 |
| Operation / MutationBatch / CommandMutation，未证明双耐久 | 失败或不确定 receipt | 空 | skipped |
| Operation / MutationBatch，双耐久 | committed receipt | 每条 Operation 一个 canonical payload | 空 |
| CommandMutation，双耐久且事件被接受 | committed receipt | 每条 Operation 一个 canonical payload | success / failed |

`events` 在共享锁内生成。HTTP、MCP、CLI、pipeline、undo/redo 必须消费它，不能在解锁后面对可能已经更新的 Document 重算 snapshot。

### 2.5 CommandMutation 与 DeferredAction

`CommandMutation.operations` 是主 Operation 在前、派生 Operation 在后的完整 logical commit；`after_commit` 是不可在 producer 内提前发生的有序运行态动作。动作只接收 commit id，不得修改已成立的 receipt。任一动作失败形成独立 `ActionReceipt(status=failed)`，其后的动作标记 skipped、不继续形成第二段部分副作用；业务仍 committed，调用方只能从失败点修复幂等 action，不能重放命令。

### 2.6 MutationSession

Document 可选提供 `begin_mutation_session() -> MutationSession`。session 在 producer 前捕获完整内存对象图，`rollback()` 必须原位恢复 Document、Entity、版本与元数据，`commit()` 接受当前图并释放快照。没有该方法的旧 fake/adapter 继续保持 W03 行为，但 coordinator 不能伪造 `rollback_succeeded`。

回滚失败统一抛 `MutationRollbackError`（ZeError code 固定为 `mutation_rollback_failed`）。producer 自身抛错时，如果回滚也失败，producer 异常保留为 `__cause__`，回滚异常保留在结构化属性/context，HTTP、MCP、CLI 因而都能输出稳定错误码而非无结构 500。

### 2.6.1 Pipeline v2 durable authority hook 与 dormant delegated check（core 3.40）

任何待提交 Operation 带 strict v2 Pipeline descriptor 时，coordinator 必须在创建 CommitIntent、history store permit 与任何 store I/O 之前调用 private authority preflight。preflight 只接受 exact sealed Operation；新 Start 每笔最多一条且必须是 tuple 首项，descriptor `initial_operation_ids` 必须与整笔 tuple 的实际 id 逐项同序全等。它从 exact FileCommitStore/OpLog fresh snapshot 取得旧 rows/head，追加本次 exact canonical bodies 后运行 complete-history reducer，形成绑定 store、OpLog、Operation objects、prospective count/tail 与 reduction fingerprint 的一次性 permit。公开/raw/unbound OpLog writer 无权直接持久化 v2；实际 append 必须再消费 seal+permit+exact OpLog+exact order 的一次性 authorization，失败或重复在 I/O 前拒绝。

store 返回后，只有 receipt 明确证明 Document/OpLog 都 `NOT_DURABLE` 才允许保留该 run 已有 attestation并安全释放 seal claim。任何一侧 durable/unknown、write-scope/confirm/freeze/session/history-context finalizer 中断，均意味着旧状态不再可证明：coordinator 必须先按 permit keys 撤销旧 attestation、永久消费本次 seal，再以 typed `outcome_unknown=True` 和只读 commit facts 阻断重放。committed+双 durable 路径还要先完成 history transaction finalizer，再用 exact store/OpLog 新取完整 rows/head；只有 count/tail 与 prospective proof 一致、每个 reduction fingerprint 重算一致，才原子安装新的 process-local attestation。new reconcile claim 后的任何 fresh/reducer/issue 失败保持零 attestation。event build/publish 在 durable reconcile 后；事件失败不改变业务耐久事实，但会跳过 DeferredAction。

permit、seal 与 attestation 都不可复制/序列化、fork 后清空，并且同一 sealed Operation 不能跨 store、跨 exact Document 或并发 permit 复用。attestation 的 keyed integrity 同时绑定 store、Document、active key 和完整 reduction fingerprint；同 id 外来 Document、成对篡改 facts/digest 或畸形 key/tag 都会撤销正反索引。它只证明“这份 durable closure 在本进程 fresh 重证过”，不进入 CommitExecution/CommitReceipt/event/SDK，也不授予 worker。

Batch 4 只新增 private、dormant 的 delegated mutation authority check。准备和消费都必须验证同一 attestation、FileCommitStore、Document、CommandRegistry 与 canonical history runtime graph；fresh snapshot 不得持 registry lock 再取 OpLog lock，必须用前后 exact graph/head 双检消除与 append 的锁序反转，读取失败冻结 runtime 并投影 typed unavailable。随后从当前完整 history 重新归约同一 Start，核对原业务 command/policy digest、work-item→entity、stage、字段/lifecycle、实体 identity/version、cardinality 与实际 worker Actor/source。请求在签发后 head 或任一事实变化即失效；claim 时从 live ledger 原子移除，篡改 detached state 不能重新武装。

这一 request 会读取完整历史中 reducer 归约出的 Control facts，只用于拒绝 stale/terminal/reconcile-required；它固定 `execution_authority=False`：不调用 coordinator/store、不 dispatch、不修改 Document/OpLog、不签 grant、不注册/启动 worker、不创建 action/manifest/run、不签发或执行/应用 Control lifecycle，动态创建也在 Batch 5 前拒绝。Batch 5 必须在同一真实 CommitCoordinator transaction 内重做这些检查并叠加 live grant registry；当前 legacy lifecycle Control/DeferredAction 不能被冷恢复事后解释为 v2 Control。

### 2.7 SQLite transaction kernel（W06.1/W06.2a）

kernel 的 `transaction_id`、`kind`、`committed_at` 构成一个最小可执行 receipt 模型。它要求：

- receipt 必须与调用方自己的 SQLite unit of work 在同一个 transaction 内写入；
- fresh reconcile 只报告新连接从数据库读到的 receipt，不依据异常 message、旧连接缓存或调用方记忆猜结果；
- `transaction_id` 唯一；重复写同一 id 不能产生第二条 receipt。schema v3 以不可变 `(transaction_id, kind)` 唯一索引为 import/catalog ledger 提供 exact composite FK binding，不能靠只比较 id 后猜 kind；
- 通用 kernel `commit` 必须在打开数据库前拒绝 import 与 catalog 的 reserved transaction prefix/kind；这些身份只属于各自受控 writer，普通 callback 不能先写一条无 payload binding 的 receipt 抢占它；
- 连接绑定创建进程/线程，fork 或越线程使用 fail closed；
- 普通初始化的 repository singleton 仍固定为 `authority=legacy`。schema v6追加17张atomic transaction私有表；V7修复revision；历史V8追加25张production authority/worker表；V9追加3张payload sidecar；V10只追加guard；V11重建mount/head约束；V12追加artifact/chunk/baseline sidecar/install closure 4张表；current V13再追加4张direct-predecessor binding表。因此`PRODUCTION_RUNTIME_TABLES=36`，两组数据面计数是`17 + 36`；fresh安装后全空，generic UoW不能使用保留identity。历史adapter只在显式冻结registry下验证；current-v13 production适配器仍无cutover/router caller，不接收Document/OpLog双写。

公开 API 的精确签名以 `sqlite_kernel.py` 为准；人读状态机、运维限制和 cutover 条件见 `SQLITE-KERNEL.md`。

### 2.8 Business snapshot schema v2（W06.2a）

schema registry 是 DDL 的唯一来源：v1 kernel migration 保留已经落盘的 legacy checksum 算法，v2 migration 使用只规范换行/外围空白、不改写 SQL literal 的精确 UTF-8 checksum。实际打开还必须把完整 `sqlite_schema` fingerprint 与 registry 对比，不能只相信 migration ledger。

v2 只新增三张当前快照表：

| 表 | 规范投影 | 关系 |
|---|---|---|
| `projects` | repository singleton、project id、generation；预留 codec/format/payload/hash | project id 主键；指向唯一 kernel repository |
| `documents` | project/document id、Document version、schema version、collaboration mode、可空 save policy；预留 codec/format/payload/hash | project id 主键，强制一项目一 Document；document id 仍按 Document 合同全局唯一；删除 project 级联 |
| `entities` | project/document id、type/id、Entity version、deleted；预留 codec/format/payload/hash | `(project_id, entity_type, entity_id)` 主键；复合外键指向 Document，删除 Document 级联 |

三表必须是 `STRICT`，payload 必须是非空 BLOB，SHA-256 必须是 32-byte BLOB；外键可延迟到事务提交检查。这里的列是 snapshot contract/adapter 的不可变落点，不表示业务行已经写入数据库：

- migration 不插入 project/document/entity 行；
- deterministic codec 仍只实现严格 JSON 值的字节级编解码；W06.2c 另以纯内存合同实现 snapshot envelope、Document/Entity 投影与根摘要验收；W06.2e 的专用 scanner/prepare seam 可以消费这些合同，但没有任何 SQLite 业务表写入；
- 三表属于 kernel private registry，普通 `UnitOfWork` 的 INSERT/UPDATE/DELETE 必须在任何 receipt 前失败；
- 不含 commit、operation、outbox、inbox、action、history 或 asset 表；
- `kernel_repository.authority` 必须继续为 `legacy`，FileCommitStore 仍是生产事实源。

历史 duplicate 若复制了相同 document id，后续 importer 必须先报告冲突并走显式修复/重写流程，不能为了让导入变绿而放松全局唯一合同。

v1→v2 这一步把 `user_version` 推进到2；current immutable registry继续执行v3–v13，包含v5 repository proof、冻结v6 transaction substrate、v7 compatibility、历史V8–V12与current V13 bindings，健康数据库最终`user_version=13`。旧kernel遇到超前schema必须fail closed；V10→V11、V11→V12、V12→V13均只接受各自规定的clean/authentic前驱；其他降级/迁移只能恢复升级前备份或另立协议。

### 2.9 Deterministic snapshot codec（W06.2b）

`business_codec.py` 是一个纯字节边界，只接受 Python 中与 JSON 严格对应的精确内建类型：`None`、`bool`、绝对值的十进制数字不超过 4300 位的 `int`（含 4300 位边界，正负号不计入位数）、有限 `float`、可严格 UTF-8 编码的 `str`、`list` 和仅含字符串 key 的 `dict`。它不会用“看起来像”的 Python 对象做隐式转换；tuple/set/bytes/自定义对象、非字符串 key、超过 4300 位的正负 `int`、NaN/Infinity、孤立 surrogate、循环引用和超过 128 层的容器嵌套都必须结构化 fail closed。这个上限属于 codec 合同，不继承 CPython 进程级 int/string digit limit，以保证 Python 3.9 与 3.13 的接受域一致并限制 CPU DoS。

`encode_payload(value)` 先验证并复制值图，再输出紧凑、无 BOM、无无关空白、object key 排序后的 UTF-8 bytes。同一精确类型和值图在同一 codec id/version 下只能产生一份 bytes（例如 `1` 与 `1.0`、`0.0` 与 `-0.0` 是不同输入），dict 的插入顺序不影响结果；codec 不修改输入对象。`decode_payload(payload)` 只接收精确 `bytes`，除了拒绝非法 UTF-8、非法 JSON、重复 key 与非有限数字，还会重新编码比对；因此带 BOM/多余空白、不同 key 顺序、非唯一转义或数字拼写即使语义相同也必须拒绝。对任何已接受的输入，必须同时成立：

```text
decode_payload(encode_payload(value)) == value
encode_payload(decode_payload(payload)) == payload
```

资源边界使用同格式的 `encode_payload_bounded(value, maximum_bytes, maximum_nodes)` 与
`decode_payload_bounded(payload, maximum_bytes, maximum_nodes)`。bounded encode 在调用正常
encoder 前逐 codepoint 计算 JSON escaping/UTF-8 字节数，并按“每个 value + 每个 object key”
累计节点；bounded decode 在 `json.loads` 前直接扫描 bytes（正确跳过 string/escape，并计入
Python JSON parser 接受后才由 strict 层拒绝的 NaN/±Infinity）。超 byte/node 必须先拒绝，
不能先分配完整 UTF-8、canonical bytes 或 parse tree。snapshot v1 对每个 envelope 同时钉住
业务 byte ceiling 与 `MAX_SNAPSHOT_VALUE_NODES=250000`；encode/decode 使用同一接受域。

`payload_sha256(payload)` 返回传入 bytes 的精确 32-byte SHA-256；它是摘要原语，不是格式认证或存量 hash 验收 API。因此未来 adapter 读取存量 payload 时，必须分别证明“`decode_payload` 接受 canonical bytes”与“重算摘要等于 envelope/数据库中的预期值”；仅调用 `payload_sha256` 不能证明 payload 合法。

`BusinessCodecError` 的异常消息只含稳定 code 与 value type，底层 JSON/Unicode 异常不得作为 cause/context 保留原 payload。结构化 `path` 为定位错误会包含 object key，外部 API、日志和遥测不得原样投影，必须按其信任边界脱敏。无界 API 只定义格式；业务容量由调用方传给 bounded API，snapshot/importer 不能把任意大值或 bytes 交给无界入口。

这一 codec 不 import/构造 `Document` 或 `Entity`，不扫描 legacy 项目，不打开 SQLite，不知道 `projects` / `documents` / `entities` 表，也不产生 `CommitReceipt`。W06.2b 自身不改 schema；current registry 已是 `user_version=13`，v2–v13 private 不变式仍然生效。W06.2c 的业务 envelope/投影是独立上层模块，不得反向塞进 codec；W06.2e 只能在专用上层组合它，生产 authority/router 仍是独立交付。

### 2.10 Business snapshot contract（W06.2c）

`business_snapshot.py` 只定义业务投影、验收与内存恢复，是后续 importer/adapter 才会消费的内部合同，不进 `sdk.__all__`。它的聚合 API 是：

```python
capture_business_snapshot(project_id, generation, document) -> CaptureResult
verify_business_snapshot(snapshot) -> BusinessSnapshotSet
restore_business_snapshot(snapshot) -> RestoredBusinessHead
```

capture 的调用方必须已持有和 Document mutation 相同的 project lifecycle/commit lock；这个纯函数不会自己发明另一把锁。输出是三类 frozen row，其字段与 adapter 需要显式写入的 schema v2 snapshot 列同序，外加不混入 canonical rows 的 read-only 排除 diagnostics。`projects.repository_singleton` 是 kernel-owned 的常量/default FK，不属于业务 envelope；未来 adapter 必须使用命名列并让数据库 default 为 1（或显式写 1），禁止 positional `INSERT ... VALUES`：

- Document envelope 保存稳定 identity、版本/协作/保存策略、name、data schema、config、ACL 与时间；不存 collections、effective policy、read-only source 或 harvest rule；
- Entity envelope 保存 identity、version/deleted、同类型集合插入位次 `collection_position`、name/data、AI provenance、actor log 与时间；不存 read-only 标记、schema 副本或 validator；位次按每个 type 的 canonical（含软删、排除 read-only）对象形成 `0..n-1`，rows 仍按 identity 排序；
- Project envelope 不复制所有业务 payload，而是绑定 `project_id + generation`、Document descriptor，以及按 Unicode/Python `(entity_type, entity_id)` 规范排序后的 Entity descriptor 数量和集合摘要。集合摘要不是整包 descriptors：它以 domain `ze.business-snapshot.entity-set.v1` 做 SHA-256 域隔离，对 domain、uint64-BE count 和每个 canonical descriptor bytes 逐项采用 `uint64-BE length || bytes` framing 后流式 update，峰值只物化单 descriptor；
- `project_id` 是 repository namespace，不被强制等于 `document_id`；行列与 envelope identity/state 必须双向一致；
- capture 包含软删 Entity；hard-remove 后因对象已不在图中而不包含；read-only overlay 必须排除并在 `CaptureResult` 中按 identity 显式报告，不能把排除悄悄当成业务删除。

capture 不对不受信的嵌套值图调用 `copy.deepcopy`/`dataclasses.asdict`。它手工组成 envelope，立即用 deterministic codec 编码，再从这一份 bytes 解码出 codec 拥有的新值图；SQL 列值、descriptor 和 hash 必须来自同一份冻结事实，禁止对活对象二次读取。

capture 还必须核对当前 Document/Entity/AiProvenance 的 exact state inventory，避免未来字段被
旧格式静默丢弃；`_collections` 必须保持 `defaultdict(dict)` 语义。canonical Entity schema 必须
与 Document data schema 精确一致：非空 schema 只接受 framework validator exact wrapper，
空/未声明 schema 只接受 `_no_op_validator`。custom callable 即使“看起来一样”也不能持久化，
因为 restore 无法确定性重建其语义；read-only overlay 因不进入 canonical rows 不受此项阻塞。

format v1 的容量天花板是：project payload 64 KiB、Document payload 16 MiB、单 Entity payload 8 MiB、最多 1,000,000 个 Entity，全部 project/document/entity payload 合计最多 1 GiB。每个上限都在进入它所保护的相应 decoder/大循环前生效：先预检 project+Document，证明根 descriptor/count 后再预检全部 Entity 与聚合总 bytes。这不是默认部署配额；真实产品可以在 importer/adapter 上层设置更严的唯一配额，不得放宽 format 接受域。generation/Document version/Entity version 都是非 bool 的精确 SQLite 正整数（`1..2**63-1`），Entity row 的 `deleted` 是精确 `0/1` 整数并必须和 payload bool 一致；时间/成本只接受非 bool、有限且非负的 int/float，不偷做数字类型归一化。provenance/actor/actor-log 都是闭集键集；actor-log version 严格递增但允许有间隔，`fields` 保持历史顺序，不排序或去重。

codec 的 128 容器层是线格式天花板，专门给 snapshot 的 schema/identity/state 包装留出余量，不会扩张 Entity/Document 的 120 层业务域。verify 在构造领域对象前，必须用同一无 hook strict clone 重证 Document data schema/config/ACL、Entity data、每个 AiProvenance prompt_args 及其十字段 envelope、整个 actor log。Document 三个字段、Entity data 与 actor log 各是独立所有权值，每个 provenance item 则按“字段深度重置+整 item 共享节点预算”验收；不伪称这些是一个跨字段的 domain 共享预算。另外，codec 对整行 envelope 的 250,000 serialized-node 天花板仍是更严的组合限制。

verify 是存量数据的唯一信任边界，严格顺序是：① exact aggregate/tuple/count；② project+Document carrier/单行 bytes 与当前合计 bytes 预检；③两行分别按固定 codec/format → 32-byte hash → bounded canonical decode（byte/node scanner 先于 `json.loads`）→ 闭集/业务域 → payload—SQL 列绑定验收；④检查 project 中的 Document descriptor 与 Entity count；⑤预检全部 Entity carrier 并完成 1 GiB 聚合 bytes 限制；⑥逐行执行与第③步相同的 hash/decode/业务验收，同时检查 identity 顺序与每 type 位次恰为 `0..n-1`；⑦重算 project root。不受信的行集不能被默默排序成“看起来正常”。删掉、多塞或只改某一合法 Entity（即使重算叶子 hash）会因旧 root 不匹配而失败；但 SHA-256 root 是一致性/部分改写检测，不是带密钥真实性证明。能一致重写全部 leaves 与 root 的攻击者不受 v1 阻止，后续 payload-bound receipt 仍需外部 trust anchor/签名边界。

restore 必须先完整 verify，再按 `(type, collection_position)` 直接构造 Document/Entity 并恢复已验证的私有版本、时间、provenance 和 actor log；不得调用会再次解释 legacy 文件或触发新业务变更的 `Document.open`、`Entity.from_dict`、`Document.add`。非空 validator 以存储的 Document data schema 通过 `attach_validator` 挂上，只约束未来写入，不借恢复路径重验历史数据。返回前必须重新 capture 并与输入的每个 canonical row/bytes/hash 精确相等，且不得出现 read-only 排除记录。

`RestoredBusinessHead.requires_runtime_rehydration` 固定为 `True`：restore 只证明 persisted
business state，格式刻意不含 harvest rules、read-only sources 或其他 runtime ports。该对象在
W06.2d 唯一 rehydrate seam 安装运行态声明前，不得发布、缓存或直接用于服务请求。

W06.2c 自身仍然是纯内存合同：它没有目录扫描、SQLite 写入入口、生产 adapter、OpLog/operation 投影、receipt payload binding 或 authority cutover，也不 import `sqlite3`/kernel、file adapter、project topology 或 sdk。W06.2e 由独立专用门面从外部组合它；不能为了已有 scanner 就把 I/O 或 topology 反向塞进 snapshot 模块。当前整体 `user_version=13`，v2–v13 全部 private 表的不变式与普通初始化的 `authority=legacy` 仍保持。

### 2.11 Runtime load coordinator（W06.2d）

runtime rehydration 套件是 snapshot restore 与未来 Context 之间唯一同步转换：private state 层独占 carrier/ledger，private projection 层独占声明与投影，`sdk/runtime_rehydration.py` coordinator 门面独占状态转换，依赖只能 state ← projection ← coordinator。三层都不进入 `sdk.__all__`，也不 import 任何 storage/topology/cache/log/event/supervisor 实现。生产调用方必须先持有 topology generation lease 与 per-project load singleflight，并独占一个从未发布的 Document；coordinator 自身再用 `begin_mutation_session(exclusive=True)` 拒绝外层 rollback token、同文档重入和跨线程 session overlap。该 session 从 baseline 重验一直覆盖到 ready 签发，不把 pending candidate、authority proof 或 mutable Document 交给调用方。

声明快照必须来自 exact `DocumentRegistry`，显式包含目标 `schema_version`、部署提供的 `runtime_generation`（core+mod build generation）、entity schema、`collaboration_unit`、collaboration mode、save policy、harvest rules、read-only source path 与 migration callable identity。carrier 只是一枚无字段 handle；声明、detached head、候选、ready 与每 Document lifecycle 的权威事实全部在 module issuance ledger 中按对象 identity 保存。该 ledger 防框架误用，不是抵抗能 import/反射任意同进程私有 Python 的沙箱。

唯一顺序是：

```text
consume rooted RestoredBusinessHead once
  → recapture == restored business root
  → reserve one Document runtime lease
  → begin exclusive mutation session
  → purge stale runtime-only overlay
  → install target schema/mode/policy/harvest authority
  → run canonical migrations (no read-only overlay exists yet)
  → reject declaration-authority mutation / false bool claim
  → validate only final new-or-changed canonical rows with target schema
  → if canonical changed: bump Document version exactly once + freeze after snapshot
  → synchronous authority CAS or canonical no-op proof
  → only after canonical durability: resolve/materialize read-only overlay
  → recapture business rows + exact runtime object inventory
  → fresh authority fence(project/generation/root/document_version)
  → final recapture + close session
  → synchronously return manifest-bound RuntimeAdmission (no ready gap)
```

历史 canonical 行没有变化时只重绑 validator，不借启动重验旧数据；migration 新增或改动过的最终行必须通过当前 schema。Migration 不得替换 schema/mode/save policy/harvest/read-only/validator authority，也不得自行推进 Document version、替换 identity 或 creation time。canonical 事实没有变化时 version/timestamp 精确保留且 authority writer 调用数为零；有变化时 coordinator 独占 version 分配，`after_document_version == before_document_version + 1`。

authority port 只接受冻结的 `RuntimeAuthorityWrite`，CAS 输入绑定 project、generation、before business root/version，输出绑定 after root/version/snapshot。typed outcome 状态机不可协商：

| outcome | 内存动作 | 是否可发布 |
|---|---|---:|
| `not_durable` 且 receipt 精确绑定 | rollback 到原对象图，释放 runtime lease | 否 |
| `committed` 且 receipt 精确绑定 | 永久禁止 rollback，继续装 runtime overlay | 仅全部完成后 |
| `unknown`、writer 抛错、receipt 非法/错配 | 关闭或保留安全 session，Document quarantine，等待 authority 重读 | 否 |
| rollback failed / commit 后 overlay failed | Document quarantine，不能把旧图冒充 authority | 否 |

只读 overlay 在 commit 之后安装，多个 source 可以共享同一已声明 entity type，但 source name 必须唯一且实体 identity 仍由 Document 拒绝冲突。每个 resolver/loader 前后都重证 canonical business 与此前 runtime inventory；后一 source 不能改名/注销前一 source、替换 loader/schema、修改前一只读行或重排行顺序。完成后再以 manifest 精确核对 source key/name/type/schema/loader identity。ready inventory 对 collections/bucket/entity/source spec 的对象 identity、source key/name、loader 强引用、schema/validator/owner、harvest 与只读 payload 做精确复核，不能用可复用的 `id(loader)` 代替对象 identity。

Generation lease 只能阻止 topology replace/delete，不能单独证明同 generation 的业务 head 没被另一进程推进；canonical root 也不包含 collaboration units/harvest/read-only manifest。overlay 完成后、session close 前必须调用 `exact_business_head_fence_v1` fresh-read port，精确重证 project/generation/business root/document version/declaration digest/runtime generation 并取得 authority revision；`advanced/unknown/非法 receipt` 均不得发布。no-op 路径同样必须 fence，changed commit 也不能把 commit 时 CAS 当成 overlay 完成后的最终可见性证明。滚动部署若没有统一 runtime generation authority，就不能让两个 worker 用不同合并粒度同时服务同一 project。

Coordinator 不向调用方暴露 pre-admission ready handle：final fence、内存重验、session close 与 admission 签发在同一次 `rehydrate_runtime` 调用内完成，直接返回 `RuntimeAdmission`，避免 caller 持有 ready 任意久后再发布已经过期的 head。同一 Document 从此永久拒绝再次 rehydrate。Admission 绑定 Document、完整 `RegistryDeclarationSnapshot`、collaboration units、project/generation/root/version、commit proof 与 final authority revision；未来 Context、CRDT/mode、CommitCoordinator、OpLog/outbox、Broker、Commands 和 Supervisor 必须只消费这一包，禁止各自重新 load registry 造成版本错绑。

W06.2d 仍不是生产迁库：W06.2e 提供 legacy loaded-head 的一次性兼容签发，W06.2f/W06.2g 建立并写入 terminal shadow，W06.2h 把完整 live catalog 封成历史事实，V5a/V5a.1 又补 admission 与 live/deleted proof；后续虽已有 strict mount/install-closure reader、V13 phase-binding semantic reader 与窄 production writer，但仍没有 production cutover coordinator、authority router 或 Context/supervisor 接线。`RuntimeAdmission` 尚未被 Context builder 原子 claim；Context 仍须从 authority revision 建订阅/outbox并追平后才可服务。Migration/loader 是可信同步 callback；exclusive session 会在任何文件 I/O 前拒绝 live `Document.save`，durable source freeze则会在所有普通legacy lease与callback之前永久拒绝，但当前进程仍无法撤销恶意callback自己进行的其他I/O。当前 `user_version=13`；普通启动仍 `kernel_repository.authority=legacy`，17张transaction表加36张production/runtime表必须为空且production runtime禁止消费。

### 2.12 Strict legacy scan 与 prepared payload（W06.2e）

W06.2e 是迁移准备边界，不是数据库 importer。它只扫描调用方明确给出的 `project_id + expected_generation`，且只接受 ProjectTopology 当前 live namespace 中已经归一到受管 project root 的项目；不会遍历 catalog，也不会追读 staging、trash、松散旧根或调用方拼出的任意目录。因此一批成功结果只证明 manifest 中 `selection=explicit_projects` 的选择，绝不证明某个时点的 repository 全 catalog 已完整覆盖。每个 handle 各自绑定一个项目的受锁时点；prepare 不持 repository-wide snapshot lease，所以多个 handle 也不代表跨项目同时刻的原子视图。

内部门面的三条可调用路径是：

```python
scan_legacy_project(topology, project_id, expected_generation) -> LegacyScanHandle
prepare_legacy_import(import_id, handles) -> tuple[FrozenLegacyImport, PreparedLegacyPayloadReceipt]
detach_legacy_runtime_head(handle) -> DetachedBusinessHead
```

private future adapter 另有 capability-guarded reservation/finish；上述模块、类型和函数都不进入稳定 `sdk.__all__`，不得让生产 surface/下游 mod 把迁移桥当长期公共 API。

单项目扫描的合法锁域固定为两层，并且 snapshot capture/verify 必须在这两层同时存活时完成：

1. 外层 topology source context 按 repository SH → project lifecycle SH → pinned live-root fd 固定 expected generation、named live root 的 `(st_dev, st_ino)` 与已验证 source head，并在退出前重证 namespace/generation/root identity；
2. 内层 Document scan context 在任何 store I/O 前验证 exact module capability 与外层 root identity，再用同一个 `EntityFileStore`、held root fd 和 Document content SH 完成 exact head、v2/raw legacy Entities 与 exact Document 重建；
3. scanner 要求外层 head 与内层 head 精确一致，在内层 yield 中 capture 后立即 verify，再在退出前复核 Document 没有 read-only exclusions。不得把 `Document`、snapshot set 或路径带出锁域后继续扫描。

成功只签发一个 opaque `LegacyScanHandle`。它可供人或后续协调器核对 project/document identity、generation/version、storage version、source-head bytes/hash、business root、Entity 数与规范行总字节；它不暴露 mutable `Document`、snapshot rows 或业务 payload，也不能序列化/伪造。源文件失败、head 漂移、identity 不一致、容量超限或 read-only exclusion 都在 handle 签发前 fail closed。

批准备接收 non-empty exact tuple 的 handles 与 64 位小写十六进制 import id；同一 handle/project 不得重复，跨项目 Document id 也不得重复，source head bytes 必须大于零，项目按 project id 规范排序。总量上限为 10,000 个 project、1,000,000 个 Entity、1 GiB canonical row bytes；manifest 另受 64 MiB/250,000 node 上限约束。虚拟 payload 不通过大 bytes 拼接实现，而按以下顺序流式 SHA-256：先对 payload domain 分帧，再分帧 8-byte project count；对每个排序后的 project 依次分帧 project row bytes、Document row bytes、8-byte Entity count 与每个已按 snapshot 合同排序的 Entity row bytes。所有分帧必须复用共享 `uint64-BE length || bytes` 原语。

canonical manifest 绑定 import id、协议、`selection=explicit_projects`、全批 counts/bytes/payload digest，以及每个项目的 generation、Document version、storage version、source-head digest/bytes、business root 与行计数。manifest digest 和 batch digest再用各自 domain-separated framing 绑定，避免同一 bytes 在不同语义中混用。`FrozenLegacyImport` 与 `PreparedLegacyPayloadReceipt(status=prepared_verified_payload)` 由同一 process-local identity ledger 成对签发；receipt 绑定 exact import/package/digests，但它的事实只到“这份内存载荷已规范构造并复验”。scan/prepared record 与所有 reservation 还绑定 exact issuer PID + epoch；fork child 必须在不接触继承锁的前提下替换锁和全部 ledger，parent carrier 不能再经受支持 seam 读取属性或取得 verified snapshots，parent reservation token 也不能 verify/release/finish；parent 副本不受影响。receipt **不是** durable receipt、commit receipt、migration receipt、SQLite transaction receipt、业务 `CommitReceipt` 或 authority proof。

一个 scan handle 有两条互不替代的一次性 lane：

- payload lane：最多参加一次成功批准备；整批失败会原子释放 reservation，不允许出现部分 handle 被消费；
- file-authority runtime compatibility lane：最多一次恢复同一 verified snapshot，并经既有 rooted restore handoff 交给 runtime rehydration；它不消费或证明 payload lane，反之亦然。

prepared package 自己还有一次性的私有 future-adapter reservation seam。它先原子 reserve exact package/receipt pair，再在 ledger lock 内结构克隆 sealed record 的全部确定性字段与 Project/Document/Entity rows，锁外重新生成/核对 manifest、virtual payload、batch digest 和每个 snapshot，最后回锁逐字段复核当前 sealed graph 并签发，排除共享引用与跨时点混合；final 回锁确认不再额外 clone 完整 snapshot 图，但峰值仍包含 sealed graph、isolated projection graph 与必要的 plan row-intent graph。验证前后都重证 PID/epoch、carrier pair、reservation token、record state 与 active membership。返回值是 opaque `VerifiedLegacyImportPlan`：顶层绑定 process-local plan protocol、import id、namespaced transaction id、receipt kind/protocol/intended terminal status、manifest/batch/payload protocol、selection、全批 counts/bytes、manifest bytes 与三摘要；每个按 project id 排序的连续 ordinal descriptor 绑定 project/generation、Document identity/version、storage/source head、business root、counts/bytes，并直接持有无 `__dict__`、不可改写、不可 pickle 的 project/document/entity row views，逐列覆盖三张 business tables。row/collection repr 只显示有界数字或 count，不打印任意业务字符串、snapshot bytes/hash，避免日志泄露与超大 repr。`snapshot` 属性只是从这些 plan-owned rows 现构造的 detached compatibility copy；调用方修改它不会改动 plan，但 writer 也不得把该可变副本当权威输入。writer 只能从 exact bound plan 的 immutable row views 取值，不得再读取 package properties 或 `reservation.record` 分段拼装，也不得接受脱离 active reservation 的裸 plan/rows。

plan 无 `__dict__`，不能直接构造、复制或序列化，也不进入 `legacy_import.__all__` 或稳定 `sdk.__all__`。active registry 以 exact object identity 绑定 `(reservation, plan)`；private importer 的最后读取入口必须重证 pair membership 与 sealed source projection，伪 plan、跨 reservation plan、fork 继承 plan 或 source 漂移均 fail closed。reserve 仍是单赢家，并强制调用方在调用 reserve **之前**创建、自持 exact process-local attempt token。registry 把 token 一并绑定，recovery 同时要求 exact package、receipt、attempt token：已签发 plan 时返回原 pair；若异步中断落在 ledger/state reserve 的返回边，registry 中只有 reservation 而尚无 plan，则 exact owner 在该 reservation 上继续完成投影、验证和绑定；若 release/quarantine cleanup 本身中断，则 recovery 先幂等完成 pending outcome，再返回稳定结果。仅知道 package/receipt 的并发者无法窃取 active reservation，也不会另签一份 plan。若 exact attempt owner 在 recovery 时发现 sealed source 或 issued plan state 已漂移，ledger 会先在同一锁域完成 quarantine 再返回稳定错误。plan 是完整 transaction row intent，不是 source/catalog/authority proof：它不生成 `committed_at`，不证明 source 仍当前或 explicit selection 覆盖 catalog。W06.2g bridge 只能依据 adapter-specific fresh reconcile 的精确结果决议 `consumed/release/quarantine`，不能按异常类型猜。所有 prepared carrier/issuance ledger 只活在签发进程；进程丢失后仍须重扫，只有已经提交的 exact terminal batch 可跨进程对账。

当前 issuance 对整批执行 O(n) row-object clone 和最终 primitive identity scan，最终 scan 位于 process-local ledger lock。上限只是拒绝边界，不是容量证明；production coordinator 上线前必须以最大批次测峰值 RSS、P95/P99 lock wait 与完成时间，并将阈值纳入门禁。若不达标，应在保持 exact binding 的前提下改成分片 ledger/流式 row plan，不能用取消最终重证换吞吐。W06.2g 已把“取得 immutable rows → binding → 单事务写入 → commit uncertainty fresh reconcile → finish”封装为不可重排 operation；仍待外围 fresh source/currentness fence。

W06.2e 自身不改 SQLite DDL/migration ledger，不调用数据库，也不写 `projects` / `documents` / `entities`。W06.2h 当时把registry推进到`user_version=4`；current registry还包含v5–v13，最终`user_version=13`。scan/prepare仍不改变`kernel_repository.authority=legacy`，也不触发authority cutover或dual-write。

### 2.13 SQLite schema v3 import ledger substrate（W06.2f）

W06.2f 只定义“未来 importer 成功提交后，跨崩溃可对账的终态导入事实应该怎样落库”，不实现产生该事实的 importer。immutable migration registry 在 v3 新增三项 schema 对象：

1. `transaction_receipts(transaction_id, kind)` 唯一索引，为账本到 kernel receipt 的 composite foreign key 提供 exact id+kind 绑定；
2. `legacy_import_batches` STRICT 表，一行对应一个 terminal import batch；
3. `legacy_import_projects` STRICT 表，按 batch 内的 canonical ordinal 保存每个明确选择的项目描述。

batch 行必须精确绑定：64 位小写十六进制 `import_id`；由它唯一派生的 namespaced `transaction_id = "legacy-import:" + import_id`；固定 transaction receipt kind、SQLite import receipt protocol 与 terminal `imported` status；manifest/batch/payload protocol；`selection=explicit_projects`；project/entity counts、canonical row bytes、framed payload bytes、canonical manifest bytes；以及 32-byte manifest/payload/batch SHA-256。project 行必须属于 exact batch，并以 ordinal 唯一绑定 project id、generation、Document id/version、storage version、source-head digest/bytes、business root、Entity count 和 row bytes；project/document/ordinal 只在同一 import batch 内唯一，不做错误的全局唯一，也不外键到会继续演进或删除的 current `projects` / `documents` 表。这样历史描述能独立留存，同时仍由 batch 外键收口生命周期。

两张表是**历史终态账本**，不是 prepared-state queue。`prepared_verified_payload`、scan handle、package carrier、reservation/consumed flag 与 process-local one-shot lane 一律不持久化。W06.2f 发布时尚无 writer，因此初始化/升级后五张表必须为空；W06.2g 才允许唯一 private importer 在同一受控 transaction 写正式业务快照、终态 batch/project rows 与 exact namespaced receipt。

v2→v3 upgrade 的前置条件也属于 migration correctness，而不是 importer repair：在任何 v3 DDL 前，三张业务表必须为空，且 `transaction_receipts` 不得已有 `legacy-import:` 前缀的 id 或 `legacy_import_v1` kind；命中固定为 `sqlite_legacy_import_migration_dirty`。任一脏行、保留 namespace、DDL、checksum 或 schema fingerprint 错误都必须回滚整笔 migration，保留原 v2 schema/receipts/`user_version=2`，不留下孤立索引或半张 ledger 表。健康升级保留 repository marker 与既有非保留 transaction receipts，并把 `user_version` 精确推进到 3。无 W06.2g terminal marker 的健康 v3 若出现任一业务/ledger/reserved receipt 行，打开即以 `sqlite_import_state_unexpected` fail closed。

五张表都进入 kernel private registry，generic `UnitOfWork` 的 INSERT/UPDATE/DELETE 必须在 receipt 前拒绝。`kernel_repository.authority` 仍为 `legacy`，生产业务提交仍由 `FileCommitStore` 完成；schema v3 的空账本不能被转换成 prepared receipt、business `CommitReceipt` 或“导入已耐久”的声明。旧二进制不能打开 v3；回退必须恢复升级前由 SQLite backup API 取得的备份，禁止修改 `user_version`、删除索引/表或清账本伪造降级。

### 2.14 INSERT-only terminal shadow（W06.2g）

W06.2g 把一份仍保持 active reservation binding 的 `VerifiedLegacyImportPlan` 写成可跨进程精确对账的 SQLite terminal shadow，但不接生产 bootstrap/runtime。共享 `legacy_import_integrity.py` 是 payload framing、canonical manifest 与 manifest/payload/batch 三摘要的唯一算法源；SDK prepare 与 core importer 都只能消费它。integrity reader 必须在进入下一项目 iterable 前检查 declared batch budget，并在一个项目实际产出 `entity_count + 1` 或超过 declared bytes 的第一项立即停止；不能完整消费巨大/无限 iterable 后才报超限。core projector 在复制 Entity rows 前先核对 tuple 长度、declared count/bytes 与全批剩余额，只保留项目级一套临时 `EntitySnapshotRow` 图完成 business verify，长期 import plan 继续引用原 immutable row views。

数据库状态是闭集，不是任意“非空也算导过”：

| 状态 | 必须成立 | 能否被生产读取 |
|---|---|---:|
| `EMPTY` | 无 import marker；三张 business 表、两张 ledger 与 reserved receipt namespace 全空 | 否，生产仍读文件 |
| `TERMINAL_SHADOW` | marker=`terminal_shadow_v1`；每个 reserved receipt 与 batch 一一对应；descriptor ordinal/order/aggregates 闭合；project/document/entity 全被 descriptors 覆盖且 generation/version/root/count/row bytes 一致 | 否，只能 exact reconcile |

kernel 全局校验不得读取 manifest/snapshot BLOB 或重算最多 1 GiB payload，但它必须在一个 WAL read/write transaction snapshot 内完成全部 schema、marker、receipt、descriptor 与 coverage 查询；autocommit 下跨多条 SELECT 会在并发追加 disjoint batch 时看到混合时点并误报 corruption。当前 v3 没有认证 aggregate summary，因此 terminal closure 是 O(全部 batch/project) 的 deep validation，不得写成 constant-time/cheap；正式热路径上线前应以新 schema 把 fast admission 与 scheduled deep scrub 分层。

core importer 的唯一顺序为：完整 plan/integrity/business preflight → fresh target classification → `BEGIN IMMEDIATE` → lock 内重新 validate/classify → fixed statement allowlist + SQLite authorizer → business rows → exact receipt → batch → descriptors → lock 内 kernel validation → COMMIT → **新连接、同一 read snapshot** 的 exact row reconcile → kernel 唯一 storage-file/WAL-sidecar 复检与 parent-directory fsync。任何 writer 只有通过同一 `_finalize_committed_storage` 才可返回 committed；receipt 已可见但屏障失败必须归类 UNKNOWN。Entity tuple-subclass 容器在入口通过 base tuple iterator 冻结，此后字段只从 base tuple storage 读取；可覆写 property/`__iter__`/`__len__`/`__getitem__` 不能让 integrity 与 DML/reconcile 分叉。normalized Entity graph 仍是 project-scoped 临时值，不形成第二份全批对象图。SQL grammar 责任属于七条 canonical plain `INSERT` 的精确 allowlist，它拒绝被 monkeypatch/拼接成 `INSERT OR REPLACE`、upsert、UPDATE 或其他形态；SQLite authorizer 是第二道边界，只负责白名单表、直接 UPDATE/DELETE 与 DDL/PRAGMA/ATTACH/transaction controls，因为 SQLite 不会把 REPLACE 的隐式冲突删除报告成独立 `SQLITE_DELETE` action。两者缺一不可。

exact reconcile 闭集是 `IMPORTED / NOT_IMPORTED / CONFLICT / CORRUPT / UNKNOWN`。同 import id、同完整内容重试必须返回原 receipt/`committed_at`；同 id 异内容、或新 id 命中已有 project/document identity，必须 conflict；互不重叠 batch 可以追加。exact rows 按 import id JOIN/ORDER BY 逐行比较，不 `fetchall` 全部 Entity。COMMIT 返回确认丢失时，只有 fresh `IMPORTED` 能恢复成功；精确 `NOT_IMPORTED` 才可安全重试，任何混合/读取失败不得按异常类型猜。

generic kernel 与 importer 对 async control 使用同一原则：`KeyboardInterrupt/SystemExit` 必须先完成 fresh outcome classification，正向结果还要通过 durability barrier，然后无论 committed/not-committed/indeterminate 都原样重抛；它们不是可转换成正常返回或 adapter retry error 的 acknowledgement loss。

terminal shadow 在 authority=legacy 期间不是通用查询源。generic UnitOfWork 必须是无 `__dict__`/connection field 的 opaque identity handle；真实 `_OwnedConnection` 只存在 closure ledger，模块不能暴露 connection getter/map，closure executor 自己完成 SQL 与结果物化。callback 返回（含异常）立即 revoke，保留 handle/bound method 不能继续使用，fork child 必须在不触碰继承锁的前提下替换 lock/map、拒绝 parent active handle。当前 v4 EMPTY substrate 上 authorizer 必须拒绝十一张 kernel-private 表的 `SQLITE_READ`，callback 即使 catch denial 也被 poison、整笔回滚；terminal/catalog marker 建立后 generic `commit` 必须在同一 writer snapshot validate 后、callback 前固定只读，只保留 private importer/sealer 的 append-only 路径。generic reconcile 对 reserved namespace 只允许 indeterminate；唯一正向证明来自 adapter-specific exact reconcile。同进程能用 `inspect` 穿透任意 Python closure 的恶意代码不是语言级沙箱可承诺的范围，正式不可信扩展必须进程隔离。

唯一 SDK bridge 固定执行：调用 reserve 前创建 exact attempt token → reserve 返回边丢失则只以该 token recovery → 写前 binding verify → core import → fresh exact reconcile + durability barrier → 写后 binding verify → `IMPORTED→consumed`、`NOT_IMPORTED→release`、其他→quarantine。reserve recovery 自身也有 acknowledgement edge：第一次 recovery 抛错必须用同一 token 再分类；若第二次取得 exact pair，先 release 再传播最新 `KeyboardInterrupt/SystemExit`，较早普通异常不得吞掉较新的 process-control。finish 同 outcome 返回确认丢失同样幂等重试。若取消发生在写区间之前可 release；一旦写入尝试开始而 fresh 分类尚未完成，必须 quarantine，禁止把一个可能已提交的 package 释放给另一目标；已有 exact 分类则先完成对应 terminal decision。bridge、capability 与结果类型不进 `sdk.__all__`，生产 server/CLI/bootstrap/mod 不得调用。

terminal batch 是“这份 explicit selection 已精确耐久”的证明，不是 repository migration receipt。W06.2g 仍缺 catalog completeness、跨项目同一全局时点、落库前 source-current fence、全量 source/catalog/target 对账、stable migration id/code digest/applied receipt、operation/inbox/outbox/action schema、Context claim/catch-up 与 authority CAS/cutover。`kernel_repository.authority=legacy` 和 `FileCommitStore` 生产事实源保持；禁止 runtime 读取 shadow、后台长期 dual-write，或把它映射成业务 `CommitReceipt`/ACID。

### 2.15 SQLite schema v4 与 historical live-catalog seal（W06.2h）

W06.2h 只追加两张 `STRICT`、append-only 历史表：singleton `legacy_catalog_seals` 与按 canonical ordinal 归属的 `legacy_catalog_members`。catalog transaction prefix/kind 刻意使用 v1–v3 generic identity 语法无法表达的私有值，避免健康旧库中的 lookalike receipt 永久阻塞 v3→v4。seal 必须绑定 catalog id、exact import id（空目录为 NULL）、canonical manifest、project/entity/source/row aggregates、source-catalog/target-coverage/seal 三组 domain-separated digest、v4 schema fingerprint 与固定 `cutover_ready=0`；member 必须逐项等于 exact import descriptor。v1–v3 DDL/checksum 是冻结历史，v4 migration 不得顺手改写旧表。

v4 合法状态只有四种：

| 状态 | 必须成立 | 含义 |
|---|---|---|
| `EMPTY` | 无 import/catalog marker，七张 business/import/catalog 表全空 | 尚无影子事实 |
| `TERMINAL_IMPORT` | terminal marker、一个或多个闭合 import batch，尚无 catalog marker | 只有显式批次证明 |
| `HISTORICAL_EMPTY` | catalog marker + singleton empty seal + exact receipt；无 business/import/member | 某个受锁完成时点 live catalog 为空 |
| `HISTORICAL_NONEMPTY` | terminal import 与 singleton catalog seal/member 全部逐项闭合 | 某个受锁完成时点完整 live business catalog 已入影子 |

任何 marker/seal/member/receipt 缺失、多余、错 kind、错 schema fingerprint、错 aggregate 或错覆盖都 fail closed。generic kernel 只做有界结构验证，不读取/重算大型 BLOB；`sqlite_legacy_catalog_exact.py` 是 importer 与 sealer 共用的唯一 byte-exact projection/reconcile。catalog 封印后 importer 的 `IMPORTED` 也必须通过该层，因此 manifest/digest/member 的等长篡改不能产生两套相互矛盾的正向结论。

ProjectTopology 的 catalog barrier 是迁移窗口的准入合同。所有受支持的普通 repository/project lifecycle 与 activity lease 先取 barrier SH；唯一严格入口按 barrier EX → repository EX 固定锁序，context 覆盖严格枚举、两轮 scan、terminal import、catalog seal 与 final fresh reconcile。枚举必须拒绝未决 recovery/journal、非空 staging/trash、非终态或错路径 state、缺失/孤立/不安全 root、marker/generation/Document identity 漂移；合法 tombstone 可留在 durable state，但 **不进入 v4 live selection 或 seal**。fork child 关闭继承 lock fd 时不得调用 `LOCK_UN`，以免解除父进程共享 open-file-description 的锁。

非空 coordinator 第一轮从全部 live scan descriptors 派生 content-addressed stable import id并写 terminal shadow；第二轮必须重新扫描、重新 prepare、持有另一条 active plan binding，并要求 descriptor tuple/import id 与第一轮完全相等才可 seal。空目录不得伪造 import，直接 seal empty。sealer 生产 API 不接受 callback、cursor 或 connection，只允许 marker/receipt/seal/member 四类固定 plain INSERT；authorizer 拒绝 UPDATE/DELETE/DDL/PRAGMA/ATTACH/transaction control。普通/异步 acknowledgement loss 都必须 fresh 分类，process-control 最终原样传播。

v4 seal 的最强声明仅限 barrier 仍持有的完成时点和 **live canonical business rows**。它不保存 repository epoch/revision、deleted tombstone、generation high-water、OpLog、assets、inbox/outbox、pending confirmation、action/pipeline job；barrier 释放后 legacy writer 可以继续推进文件 authority。结果必须固定 `authority=legacy`、`historical_fenced=True`、`cutover_ready=False`，不得公开到 bootstrap/runtime。v5 必须先补耐久 revision + 全 writer admission fence + tombstone/generation 水位 + 独立 cutover proof，才可设计 production authority CAS。

### 2.16 V5a repository admission 与 schema v5 proof substrate（W06.3a）

`repository-admission.json` 是文件 authority 的唯一持久准入栅栏：不变 `authority_epoch` 区分 repository identity，单调 `revision` 防止 stale writer，状态只允许不可逆的 `legacy R → cutover_pending R+1 → sqlite R+2`。pending 前的失败等于尚未离开 legacy；pending 一旦耐久，恢复只能向前，绝不重新开放文件 writer。所有普通 repository/project/activity/copy/metadata 入口必须在任何 repo/project/Document I/O 或业务 callback 前验证当前是 `legacy`。SQLite 路径必须先持 barrier SH 下的 exact authority lease，逐项绑定 epoch、cutover id 与 `source revision + 2`，再进入数据库事务；该 lease 不取得 repo lock，也不授予任何 legacy path I/O。栅栏首次安装用 owner-safe、single-link、0600 的 `prepared → record → committed` 记录，任一边崩溃后只能继续同一 epoch；已安装 marker 存在却 record 缺失/损坏时不得复活。只有 production `Repository` bootstrap 可在 barrier EX 下将严格闭合的 pre-v5 store 升级。

private strict repository snapshot 在 barrier EX → repo EX 内复用 v4 live catalog 验证，再加入每个 deleted state 的 project id、generation high-water、operation id；live 成员另绑 exact Document bytes/hash。schema v5 只追加 `legacy_repository_proof_control / legacy_repository_seals / legacy_repository_members` 三张 `STRICT`、append-only、private 表；live member 精确 FK 到 v4 catalog descriptor，deleted member 必须没有任何 live FK/payload。纯 integrity 层将 repository epoch/revision、runtime generation、code digest、live+deleted 闭集和 v5 schema fingerprint 做 domain-separated 绑定。

这三张表保留 `repository_control` 的未来语义，但当前故意命名为 `legacy_repository_proof_control`，避免让读者以为它已是可更新的 production authority row。全部持久头只能是 `authority=legacy`、`authority_state=cutover_prepared`、`cutover_ready=0`；private exact writer 可以写入并 fresh reconcile，但没有 production identity issuer/挂载 coordinator，也没有 pending→sqlite 的合法生产 caller。下一阶段必须用 exact source revision + v5 proof + target transaction state 做独立 CAS，而不是改写这份 append-only 历史证据。

### 2.17 V5a.1 same-barrier exact repository proof

core issuer 只接收重新规范化的 `CatalogExactProjection` 与按 project id 排序的 typed `LegacyRepositoryIntegrityMember` tuple。live 成员必须逐项等于对应 catalog ordinal 的 generation、Document identity/version、storage version、source hash/bytes、business root、entity count 与 row bytes；deleted 成员必须只保留 tombstone operation、generation high-water 和空源摘要。issuer 重新派生 v5 integrity 与 schema fingerprint，raw dict、伪 carrier、错序、漏项或多项全部 fail closed。

repository writer 的唯一写集合是 proof control、reserved receipt、seal、members 四类 fixed INSERT。写前和 COMMIT 后 fresh reconcile 都在单一 SQLite snapshot 内重证 v4 catalog/import/business coverage、全 v5 namespace 与 receipt 闭集；同一 projection exact retry 保留原 receipt，不同健康 projection 是 CONFLICT，同 id 任何字段不一致或部分 namespace 是 CORRUPT，I/O/busy/耐久未知是 UNKNOWN。普通 return-edge loss 只有 fresh `SEALED` 可恢复，process-control 必须 fresh 分类后原样传播。

SDK coordinator 从 S0 到 S2 持有同一 barrier EX → repository EX。namespace ABSENT 才运行旧 v4 writer；PRESENT 时只做两轮当前源扫描，在 active reservation 内构造 core projection并于 `finally` 显式 release，禁止调用已经不理解 v5 receipt 的旧 v4 顶层 reconcile。S1 在 proof 前重证源等于 S0；v5 seal 后无论成功或异常都执行 S2。最终结果不得含路径、handle、plan、projection 或 capability，不得修改 admission，并固定 unmounted/production-callers-zero。

### 2.18 V5b.1 atomic contract 与 schema v6 unmounted substrate

Document 的 private-SQL 接缝只允许 exact private capability 在同一进程、同一线程的 exclusive mutation session 内签发一个 opaque candidate。它只把内存 version/timestamp 精确推进一格并绑定 before/after head；不能复制、序列化或离开 active session 复验。确定未提交时 session rollback 必须原位恢复完整对象图，只有受控 private writer 的 exact committed 结果才能 commit session。candidate 不做 I/O，也不是 durability receipt。

`AtomicCommitPlan` 是 future writer 唯一允许接受的 typed projection。issuer 必须重新验证 rooted after snapshot、canonical Operation body/hash chain、每条 Operation 对应的 outbox、可序列化 durable action 与 pipeline intent、schema-aware asset exact occurrence、可选 inbox claim 与 confirmation consumption，再把全部计数、摘要、operation tail、candidate business head 绑定进 canonical manifest。普通 pipeline intent 只允许不创建/结束 worker attempt 的控制状态边；运行、完成、失败与恢复必须由后续 typed worker contract 携带 lease/fence/attempt 事实。writer 接收前必须从 plan 的所有成员重新构造 closure；普通不可变保护被底层绕过后的任何篡改也必须失败。integrity 层不得打开数据库/文件、读取 Document runtime 或调用外部 effect。

schema v6 在冻结 v1–v5 checksum/DDL 后追加 17 张 `STRICT` 私有表：runtime control、repository project/current Document head、append-only commit/operation/target、inbox/outbox/dispatcher control、confirmation、durable action/attempt、pipeline job/attempt/transition、asset metadata 与 commit-scoped exact occurrence history。head finalization 会逐组核对 Operation/outbox/action/pipeline/asset 实际行数，历史 asset ref append-only；当前 head 只选择当前 commit，因此 V5b.1 的 GC 必须 fail closed，后续 retention/compaction 是释放历史资产的前置。migration 只创建 DDL，不插 runtime control、head、commit 或 receipt；fresh/upgrade 后逐表为空，`kernel_repository.authority=legacy`。generic kernel 在 callback 前拒绝 v6 保留 identity。此阶段明确是 substrate，不是 writer、reader、dual-write、cutover 或跨设备同步。

### 2.19 V5b.2 schema-v7 local revision、private writer/reconcile 与 rooted reader

v6 是已经发布的历史迁移，其 checksum 必须永远保持 `41fe4ba13a35bacfa75af648381c0dadc4d256a8872c65a1bbf0138b64252254`。V5b.1 把 `repository_projects.project_revision`、`document_commit_heads.head_revision` 和 `business_commits.repository_revision` 绑定为同一个数，这在只有一个 project 时看似成立，但一旦项目 B 推进 global，项目 A 的 local head 并未变化；若 writer 仍要求 A local 等于 global，A 就永久无法提交。不得修改 v6 DDL/checksum掩盖这个错误，只能追加 schema-v7 `business_local_revision_separation_v1`，其最终 checksum 固定为 `d96606b2d6b47ee42e53dd216e7c711e00c877573814c4cc73526004e8aa0656`。

三条 revision 的权威含义固定为：repository revision 是整个数据库的连续提交序号，project revision 是某个 `(project_id,generation)` 的连续状态序号，head revision 是某个 `(project_id,generation,document_id)` 的连续 head 序号。每笔业务提交把 global 加一，同时只把目标 project/head 的 local 各加一。`business_commits` 同时记录三者；`repository_projects` 以 `(project,generation,project_revision,root)` 绑定 commit，`document_commit_heads` 以 `(commit,project,generation,document,version,head_revision,root/tail/assets/counts)` 绑定 commit。reader 对当前 head 要求 local 精确相等，runtime global 只需不小于该 commit 的 global revision。

v7 upgrade 先创建临时 preflight guard。空 transaction namespace 可直接升级；旧 private prototype 数据只允许“单 project/head、commit global 从 1 连续、current project/head/runtime 全部指向最后 commit”的形态，随后才把历史 global 值回填到新增 project/head revision、重建 Document head FK 和 closure triggers。任何多项目、断号、错 head、错 root 或不完整状态都必须在 schema 改写前整笔失败。这个窄兼容域是合理的，因为 v6 从未 production-mounted；v7 不是任意损坏数据库的 repair 工具。

change commit manifest 的 before/after 必须同时绑定三条 revision，因此 protocol、manifest schema 与 manifest hash domain 都显式升为 v2，不能让同一个 v1 标识换含义。bootstrap baseline 不是 change commit；`business_baseline_contract.py` 固定 baseline v1 schema/protocol 与独立 hash domain，future cutover issuer 与 rooted reader 必须共同消费它。

`sqlite_business_runtime.py` 只负责 transaction fast fence：在调用方的同一 snapshot 中验证 current schema fingerprint、安全 PRAGMA、SQLite authority marker、唯一 runtime control、canonical 时间/identity 与 indexed newest revision；空 commit history 固定要求 repository revision 1，不扫描历史 page、全库 FK 或 receipt namespace。`sqlite_business_audit.py` 是独立的 startup/periodic full-audit 叶：它先取得同一 fast fence，再执行完整 `PRAGMA integrity_check(1)`、`PRAGMA foreign_key_check`、流式 reserved receipt↔commit 双向闭包、global/project/head 连续 revision、live project/head 双向 current closure 与 newest-global closure，并用 main-file inode + final fresh profile 做 publish fence；`inspect_business_runtime` 仍显式代表 startup full。writer access 只接受 `active`；reader 可接受 `active/read_only`，`repair_required` 永不产生健康 head。有限 inode 采样不能抵御无视锁的恶意反复 A→B→A，production adapter 必须在整个 full audit 持 repository/maintenance lease。

`SQLiteBusinessTransactionWriter.commit(plan)` 必须先完整 `_require_atomic_commit_plan`，再以一个 `BEGIN IMMEDIATE` 验证 active authority、before global/project/head/root/tail、所有将占用的 identity，在同一事务里写 current v2 snapshot、business receipt/commit、Operation/target、outbox、durable action、pipeline intent、asset ref、可选 inbox/confirmation 和三个 current head，最后逐组重算 closure。SQL COMMIT 返回后仍必须新开连接 fresh reconcile 并通过数据库/WAL/父目录耐久屏障，才返回 committed。pre-COMMIT exact rollback 是 aborted；head/identity 冲突是可解释失败；部分或篡改 durable evidence 是 corrupt；无法完成 fresh 分类或耐久屏障是 unknown。process-control 信号不能吞：提交前回滚后原样传播，提交后先 fresh 分类再原样传播。

`SQLiteBusinessReader.read_current_head` 的第一个 `BEGIN` 是唯一 read linearization point：在该 snapshot 内读取并重证 runtime、project/head、commit/receipt、v2 snapshot、Operation/targets/outbox/actions/pipelines/assets、inbox/confirmation 与 baseline/change manifest，返回 detached frozen `SQLiteBusinessHead`。读取结束后的第二个 fresh snapshot 只确认 authority epoch/cutover/runtime/code/control 没有不兼容变化，且 global revision 未倒退；其他 project 或同 project writer 若在线性化点后提交，本次可返回旧 head，下一次才返回新 head。禁止为了“看起来最新”跨 snapshot 拼接 new runtime + old head 或隐式 fallback 到 legacy files。

这些模块仍是 private/unmounted：server、bootstrap、MCP、公开 persistence、CommitCoordinator、PipelineSupervisor 和其他 production Python 模块不得 import/call，稳定 SDK/public lock 不得导出。test-only baseline helper 只构造真实闭合的 active fixture，不是 cutover API。V5c.1 在 cutover-v1 之后增加三条互不冒充的纯链：asset stage逐字节验证bounded blob/metadata/occurrence，并把project/blob/occurrence冻结为唯一canonical SQL member projection；repository coverage只消费durable-reader盖章的exact stage；audit certificate用独立adapter/reader/ledger-reader capability绑定full-audit/schema/migration-ledger/high-water与singleton predecessor head；activation v2只消费fresh final admission、exact read-only install、fresh INSTALL audit与同一registry/barrier目标ledger。文件源还必须先在严格EX租约下安装绑定deployment identity的durable freeze，普通legacy入口此后在I/O前永久失败。mount必须证明`coverage ≤ install ≤ audit ≤ activation ≤ mount`，五阶段head再用独立domain把`installed → assets_covered → audited → activated → mounted`逐态链接；阶段2是在install时纳入先前coverage，不倒置durable receipt时间。historical audit membership只可恢复已提交activation，不能授权ABSENT/apply。所有plan都不写库、不切authority、不执行provider，盖章capability是trusted-process seam、不是密码证明。未来production coordinator必须在current schema-v13上用唯一canonical migration-ledger hash、fixed registry+barrier adapter、durable source freeze、fresh install-closure reader、`sqlite_cutover_phase_binding_reader.py`重建的direct-predecessor链、前驱receipt/fresh reconcile与同事务head/runtime CAS实现这些事实；V11只接纳空legacy production namespace，不承担历史mounted V10原地升级。百万行RSS/P95/P99门与Context/admission/outbox/subscription一致消费完成前，进入freeze/pending后只能向前修复，不能开放legacy writer。

### 2.20 V5c.1 纯前置模块的唯一职责与消费顺序

`business_cutover_asset_contract/integrity` 只回答“哪些精确 bytes 已被验证、哪些 repository member 被这些 durable stage 完整覆盖”。stage plan 允许携带 bytes，但受 512 MiB hard ceiling；coverage plan 不再携带 blob，只聚合由 durable reader 盖章的 stage evidence。相同 SHA 可以物理去重，但任一 occurrence 的长度、media type 或 extension 不一致都必须失败。它不负责移动文件、写 SQLite 或签发 authority。

`repository_audit_contract/integrity` 只回答“一次完整审计观察是否足以形成链上证书，以及某张旧证书是否仍是当前 head 的真实祖先”。full-audit adapter 负责把外部受锁审计结果盖章，durable reader 负责当前 certificate row/head，ledger reader 负责有界历史路径；三种 capability 不可互换。current-head classifier 服务写前 CAS，historical classifier 只服务恢复，不能用祖先身份替代当前写权限。

`business_cutover_activation_contract/integrity` 是唯一把 file admission、cutover install、INSTALL audit、目标 schema/ledger 与 runtime state transition 合成一个 activation-v2 事实的模块。issuer 形成期望，durable reader 形成 fresh observed state，classifier 才决定 absent/exact/conflict/corrupt/unknown；任何裸 dataclass、plan.evidence 或 v1 join 都没有授权含义。

`worker_execution_contract/integrity` 只定义后台执行事实；`PipelineBusinessControlPlan` 只定义业务侧 create/pause/resume/cancel eligibility。业务提交不得制造 claim/lease/effect/terminal，worker 也不得直接改 PAUSED eligibility。V8 冻结、V9 继承的完整消费顺序是：业务事务先持久化 intent/control；dispatcher/worker 再以 current-state CAS、lease/fence 与 exact request identity 推进；外部 effect 之前先持久化 authorization，返回后以 durable proof complete/reconcile。当前 fixed-SQL 已落地通用业务事务中的 pipeline/action/multi-outbox genesis、genesis worker ledger 上第一次 OUTBOX claim，以及同一窄 OUTBOX 垂直上的 `AUTHORIZE_EFFECT / COMPLETE / RECONCILE`；它们仍没有 dispatcher，也不包含 `RENEW / EXPIRE`、后续 RETRY claim或 pipeline/action worker execution。

### 2.21 V5c.2→V5c.3 最终授权、原子控制与 canonical durable identity

V5c.1 的 install、asset coverage、audit certificate 和 activation 各自只能证明自己的局部事实，不能把“几个 plan 都存在”解释成生产授权。`business_cutover_mount_contract/integrity` 因此成为唯一最终组合器：它只消费同一个 cutover-v1 的 read-only install、由 durable reader fresh 盖章且为 `COVERED_EXACT` 的 repository coverage，以及 fresh 分类为 `EXACT` 的 activation-v2。V5c.3 将 mount protocol/manifest/hash-domain/receipt kind显式升为 v2：install与coverage各自完整 generic parent `(transaction id, kind, committed_at)`都进入manifest，issuer还必须收到两份由不同durable-reader token签发的fresh linked-receipt observation。v8 mount row把两组kind/time投影为显式列并以trigger闭合specialised行和generic receipt；旧v1 mount、父子同时改时间或只在JSON里伪造字段都不能授权。manifest其余部分继续绑定runtime/code、source/pending/final admission、coverage member root/count/bytes、activation、audit certificate/tail和migration ledger。v1 join只保留为早期预期描述，永远不能作为active writer输入；旧wrapper、旧plan evidence或historical audit ancestor不能替代current durable facts。

`business_atomic_write_contract/integrity` 解决另一类组合漏洞：`AtomicCommitPlan` 与若干独立合法的 `PipelineBusinessControlPlan` 并排出现，并不天然证明它们属于同一笔提交。组合器要求 intent 按 ordinal 一一对应，不允许缺、重、换序；逐项核对 authority、project/generation/Document、job/pipeline/intent type、状态与 revision、replay/idempotency、max attempts、origin commit、payload/key、transaction id 和 committed time。结果manifest绑定atomic closure、全部control bindings和ledger before/after。它不打开SQLite，也不把prepared事实说成committed；production writer必须在current schema-v13的同一个`BEGIN IMMEDIATE`中fresh-read完整job before state，写全部业务/control/receipt事实并fresh reconcile。

worker protocol v3 明确区分“计划过一次”“当前状态已改变”和“改变属于哪条耐久链”。specialised receipt 绑定 operation、target、before/after status+revision、request digest、after-state digest、generic parent、sequence、previous tail、new tail 与时间。若数据库 head 已超过该 receipt，reader 必须提供有界且 fresh 盖章的 `WorkerLedgerMembershipEvidence`；仅凭 `head.sequence > receipt.sequence` 不足以判 EXACT。pipeline control 与 worker execution 共用一条 ledger 以获得全序，但 issuer 仍分离：业务控制不能伪造 worker attempt/effect/terminal，worker execution 不能绕过业务 eligibility。

outbox 的两个游标必须分开解释。`published` 表示最后一个已经有终局投递证明的连续事件，`inflight` 表示当前正在尝试的直接后继。只有 PENDING 可首次 claim 并设置 inflight；RETRY 必须保留同一个 inflight，不能在状态丢失后重建。PUBLISHED 或带外部 effect/terminal 强证明的 DEAD 可以推进 published；还有唯一一条无副作用终局：租约已经到期、effect/result 均为零、attempt 恰好耗尽时，`EXPIRE→DEAD` 也可精确推进。任意其他无 effect/proof DEAD、`OUTCOME_UNKNOWN`、跳过 pending、替换 inflight 或无法在同一 snapshot 证明 no-gap 的变化都不得推进。这样 ack 丢失会进入 reconcile，而不会重复发送或跳过事件。

schema-v8 之前先冻结两条不可分叉的数据库 identity。migration ledger root 只取连续 version、exact name、checksum scheme 和持久化的小写 64 字节 checksum，用固定 domain 与 uint64 length framing；`applied_at` 不入根。receipt namespace root 在同一 SQLite transaction 中按 UTF-8 `BINARY` transaction id 顺序流式读取全部 generic receipt，绑定 count、id、kind 与真实毫秒 UTC；空 namespace 固定为 32 个零字节。SQLite 连接必须显式验证 `PRAGMA encoding=UTF-8`，避免 Python UTF-8 byte order 与 UTF-16 数据库排序不一致。`sqlite_schema` 是 migration root 的唯一算法调用者，`sqlite_business_audit` 是 receipt root 的唯一调用者。

full audit 不能先返回 profile，再由证书 adapter 另开连接补读两条 root；那会把不同 snapshot 的事实拼成一张看似完整的证书。私有 `_audit_business_runtime_connection` 因此要求调用者已经持有 transaction，并在同一 snapshot 中返回 runtime profile、`user_version`、schema fingerprint、migration count/root 与 receipt count/root。公开 startup/periodic wrapper 目前仍只投影原有 profile；未来唯一 current-registry audit-certificate adapter 必须在同一个 maintenance lease + transaction 内消费完整 observation、读取 worker/install/coverage high-water 并签发 audit certificate。

历史 schema-v8 把 authority/worker 耐久形状落成 25 张 `STRICT` 表；历史 schema-v9 再追加 3 张 payload sidecar；历史 schema-v10只用时间/dispatch guard封闭旧SQL三值逻辑缺口；历史 schema-v11只在空legacy production namespace重建mount/head约束；历史 schema-v12再追加 4 张 install-closure 表；current schema-v13继续追加4张direct-predecessor binding表。strict fresh mount/install-closure reader、`sqlite_cutover_phase_binding_reader.py`、production writer、纯worker/asset-member/phase-binding projection、首次OUTBOX CLAIM、三操作sidecar writer、plan-free worker durable auditor、3.27.0 private install writer、3.28.0 global cutover durable auditor与3.31.0 private asset-provider writer都已存在，但这仍是production-router-zero。普通启动要求legacy marker、无runtime control、17表与36张production/runtime表全空；旧v7/v8/v9/v10/v11 adapter只能在显式冻结的历史fixture中执行。production writer只会在显式调用且plan/projection/fresh closure全部成立时写V8 mirror与V9 sidecar；install writer则只消费V12 closure projection、只读核对V3 terminal-shadow业务行并原子写install-owned闭包。global cutover auditor已覆盖零post-install suffix下的完整cutover closure，并把V13 binding语义链纳入自证；合法preinstall provider与post-mount业务/worker suffix仍保持UNKNOWN。audit/activation/mount三个阶段writer、cutover coordinator、server/bootstrap/CommitCoordinator/PipelineSupervisor caller、持续dispatcher和一般worker transition仍不存在。`GATE-DB-16/17`保存历史V8/V9，`GATE-DB-18`保存V10 hardening；V11另以空namespace preflight拒绝改释历史mounted数据。这些窄写入/审计垂直不等于系统已经切库。

### 2.22 V5c.3 schema-v8 与首条 production atomic write 垂直

下文的 “current V10” 保留该历史发布语境；3.28.0 发布当时的 current registry 是 V12，而现在的实际 current registry 已是 V13，V8–V12 仍作为逐字不变前缀存在。

`production_authority_worker_runtime_v1` 是历史 schema-v8 的唯一追加迁移，现在仍是 current V10 registry 中不可改写的前缀。它在执行任何 v8 DDL 前建立 preflight，只接受 pristine legacy-v7：17 张历史 transaction 表全空、authority 仍为 legacy、runtime control 与 reserved receipt namespace 未被占用；否则整笔回滚。迁移本身不写 runtime、mount、target 或 receipt，也不翻 authority。冻结 identity 为 migration checksum `f6c8a8e1f2aa633a52da52a04e6b58ad11230f5093e4b883e7b63674bb8ad80b`、schema fingerprint `0d96ffa4e88ac3c3d3fd1ba1b6f09f11a3a2d7a0e1861f5781bf0856d7fce771`、migration-ledger root `9595ea544b5b055f71d2c93619e49b3353a5d48424951fc410b0ca2484c1834a`；旧二进制看到 v8 必须 fail closed。

25 张新表分成四组，而不是一堆可随意组合的状态：第一组保存 install、asset stage/coverage、audit certificate/head、activation 与最终 mount；第二组保存 atomic-business outer receipt；第三组保存 worker target/current、specialised receipt、result/proof 与 shared ledger；第四组保存全局 outbox allocation 和 published/inflight dispatch head。target 创建必须同时得到 immutable target 与 revision-1 current；每次 worker 状态变化必须把 generic parent、specialised receipt、previous/current state 和 ledger predecessor/tail闭合；allocation 与 dispatch 只能沿连续直接后继推进。trigger 禁止 no-op hash churn、receipt 复用、跳号、换 inflight、漏 attempt/effect/result 与不合法 terminal。lease-expiry 的业务判断来自已验证 plan/fixed writer，schema 只闭合持久行关系，因此不依赖 SQLite JSON 函数，最低版本仍为 3.37。

`sqlite_production_authority.py` 是 mounted authorization 的唯一 fresh reader。它不能只检查表间 JOIN，而要在同一个 transaction snapshot 内解码并重建 install/coverage/audit/activation/mount typed evidence，按每个原始协议的 hash domain重算 manifest/tail/findings，逐字段核对 projection、generic receipt parent、schema/migration identity、runtime/code 与时间，最后才签发普通代码无法构造的 binding。planning snapshot 必须同时取得 current worker-ledger/outbox-allocation/dispatch high-water；新提交时间不能早于任何已经耐久的相关 head。

`production_atomic_write_integrity.py` 在 frozen `AtomicBusinessWritePlan` 外再闭合 fresh mount、每个 pipeline/action/outbox worker target 的 genesis 状态与全局 outbox allocation。3.23.0 起，`SQLiteProductionAtomicWriter` 消费纯 plan 能表达的完整三组 target：在一个 `BEGIN IMMEDIATE` 内再次核对 mount 与 allocation currentness，使用固定 SQL 和严格 authorizer 写历史业务 mirror、outer receipt、全部 worker target/current、pipeline control intent/generic+specialised receipt/atomic member、shared ledger与同 commit allocation batch，最后才推进两类 head和 runtime revision。CREATE 建新 pipeline target/current，transition只 CAS 已有 target。COMMIT 返回后另开 fresh connection重证完整事实并经过统一 storage durability barrier；pre-COMMIT 全回滚、exact retry、ack-loss、reserved identity/head conflict、durable corruption 与 unknown各有稳定分类。

fresh reconcile 对已经推进的 worker current 必须重算 canonical worker manifest、after-state、receipt 和 ledger tail，并验证 receipt/ledger/allocation/dispatch 的连续链；不能仅凭“current revision 更大”报告 COMMITTED。schema/纯合同仍能证明无副作用的 10 轮 `CLAIM→EXPIRE` 与最后 `EXPIRE→DEAD` 的合法形状；应用层 fixed-SQL 现在实现第一次 `CLAIM` 及 OUTBOX `AUTHORIZE_EFFECT / COMPLETE / RECONCILE`，并由 plan-free auditor 重建这些操作的 canonical payload/projection/V8+V9 mirror 与全局链。`EXPIRE` 仍明确不生成 result/proof projection，`RENEW / EXPIRE`、后续 RETRY claim和pipeline/action worker execution仍未进入支持面。下一阶段是建立持续 dispatcher、扩展其他状态边、长期 authenticated checkpoint 与 authority router。

### 2.23 V5c.4 Worker payload 纯投影与首次 OUTBOX CLAIM 闭环

`worker_result_projection_contract.py / worker_result_projection_integrity.py` 不定义第二套 worker 状态机，也不打开 SQLite、文件、时钟、provider 或 scheduler。它只接受 `_require_worker_execution_plan` 可完整重建的 sealed plan，把状态机已经认可、但未来 plan-free reader 仍需要的 exact payload bytes 投影出来。存在矩阵不可协商：`AUTHORIZE_EFFECT` 只有一份 effect-request projection；每个 `COMPLETE` 与 `RECONCILE` 都有一份 result projection；`RECONCILE` 总有 proof projection；`COMPLETE` 只有 sealed request 真正携带 effect link、durable result 或 terminal proof 时才有 proof。所有 `EXPIRE` 都不在此协议中伪造 result/proof；effectful expiry 的事实来自先前 effect request、当前 worker receipt/state 与后续 reconcile，而不是凭空补一行结果。

三类嵌套 payload 各自只有一个所有者：effect projection 拥有 effect request；result projection 只拥有顶层 result/error；proof projection 拥有 evidence、provider receipt 和 durable result。canonical metadata 只记录 schema、codec、长度与摘要，不复制 raw payload；component digest 再按 role 对每个 `schema / codec id / codec version / exact payload bytes / payload sha256` 做 uint64 length framing，并以分次 hasher update 流式吸收。已有 durable result id 必须沿用；否则 result id 与 proof id 用不同 domain 从 request transaction、target、operation、after status确定生成。`proof_id` 自身进入 proof metadata 与 aggregate digest，任何 raw bytes、id、binding、receipt 或 closure 改写都会在重新 `_require_worker_result_projection_plan` 时失败。

纯模型中“没有 proof”始终是 `result.proof_sha256=None / closure.proof=None`。冻结的 schema-v8 `worker_results.proof_sha256` 不能为 NULL，所以唯一兼容映射是 domain-separated 哨兵 `f14fd90bc57f6d54c068aff36091f91a2853b2b4ffa48c148a3a46d8854a3a47`；它只由 `_v8_worker_result_mirror_proof_sha256` 在完整重验投影后返回。该值不能构造 `WorkerProofProjection`，也不能作为 result 中的真实 proof pointer；未来 nullable schema 应继续保存 NULL/None，而不是把兼容哨兵永久升级成业务证明。

`SQLiteWorkerClaimWriter` 只接收 genesis worker ledger 上的 exact `OUTBOX / CLAIM / PENDING revision 1 → LEASED revision 2 / attempt 1`，并要求该 target 是 published cursor 的直接后继、before 没有 inflight、after inflight 正好指向它。它在一个 `BEGIN IMMEDIATE` 内先重验 active mounted authority、origin outer target/genesis、allocation、event mirror、worker/dispatch head与数据库时间，然后固定 INSERT `transaction_receipts / worker_ledger_entries / worker_execution_receipts / worker_attempts`，固定 CAS UPDATE `event_outbox / worker_target_current / outbox_dispatch_head / worker_ledger_head`，最后在同一 transaction 内重验 closure 才 COMMIT。authorizer 只允许这四张 INSERT 表、四张 UPDATE 表及精确列；effect/result/proof 行数必须保持为零。

租约是否已过期、`occurred_at` 是否来自未来都以同一 SQLite 连接/事务内读取的数据库时间判断，不信任进程墙钟；写完并重证 closure 后还要在 COMMIT 紧前再次取时，长事务不能提交已经过期的 lease。pre-COMMIT 普通失败只有在 fresh absence 加所有 owned surface 仍为 genesis时才返回 `NOT_COMMITTED`；COMMIT 后 ack-loss 重新读取可恢复 exact，storage durability barrier失败只返回 UNKNOWN。reconcile 会 bounded decode canonical pure atomic manifest，重算 atomic manifest/receipt 与 pipeline/control/worker-ledger根，并逐字段重建 event 的 operation/predecessor/count/topic/partition/payload immutable row；随后再闭合 outer target、current/dispatch/ledger/allocation。因此双表 topic/partition、event predecessor/count/operation、atomic receipt root、partial absence、head predecessor/revision或同长 payload任一篡改都会被发现；BUSY/I/O 或 mount 中途不可读保持 UNKNOWN。3.19.0 当时，shared worker ledger 只要出现 claim writer自身无法 plan-free 重建的 worker suffix，即使旧 receipt membership成立也只能 UNKNOWN；3.21.0 已由 §2.25 的 durable auditor为受支持后缀补上精确重联。claim writer本身依然没有 dispatcher loop，也不负责 renew、worker RETRY状态边、effect/complete/reconcile/expire；同一 claim transaction 的 exact retry与ack-loss fresh恢复属于已经闭合的幂等路径。

### 2.24 3.20.0 schema-v9 Worker payload sidecar

本小节中的 “current V10” 同样保留3.22.0历史语境；V11/V12都没有改变这里冻结的17+25+3表所有权。

`worker_payload_projection_sidecars_v1` 是历史 schema-v9 的唯一追加迁移，它不修改 V8 的任何 DDL 或 identity。V9 migration checksum 固定为 `23018e6b59b8d3ad235e3110abd122ba12584cee272cff0614730f4d4be0dba0`，历史 schema fingerprint 为 `ccb5b4d248c2c429c0a99a5811816521cf4563de3bee457db826b4e4d4090ddf`，历史 cumulative migration-ledger root 为 `32ade765a739bca3427b4901fa4eee6a737d9d4a0519c369edd2deea56d7b26a`。current V10 的表所有权仍是历史 17 张 transaction 表，加上 V8 的 25 张 production 表和 V9 的 3 张 sidecar，即 `17 + 28`。

三张 `STRICT` sidecar 分开保存 effect request、result/error 和 proof evidence/provider receipt/durable result。每一份 payload 都带 exact schema、codec id、codec version、raw bytes 和 SHA-256 五元组，再绑定 target、attempt、effect/receipt、projection manifest 与 closure。可空五元组必须“五项全空”或“五项全有”，result 与 error 不能同时出现。V9 result 的真实 proof pointer 可以为 NULL；V8 历史表为了满足 `NOT NULL` 而使用的兼容哨兵 `f14fd90bc57f6d54c068aff36091f91a2853b2b4ffa48c148a3a46d8854a3a47` 仍只是存储映射，在 V9 中明确禁止冒充真 proof。

触发器保证“先写完证据，再推进 head”：AUTHORIZE_EFFECT 必须已有 effect mirror 和 effect sidecar；COMPLETE/RECONCILE 必须已有 result mirror 和 result sidecar；RECONCILE 必有 proof，COMPLETE 则严格按纯投影的有无 proof 矩阵闭合。三张 sidecar 全部 append-only，旧 V8 mirror、sidecar 与 ledger-head 之间有 preadvance 和 exact binding guard。这些规则能发现漏行、错 receipt/attempt/effect、部分 NULL、事后补投影与错误 proof 存在矩阵；它们不替代应用层重算 canonical bytes/hash，因此 3.21.0 的 plan-free auditor 仍必须从原始行重建并逐项比对，不能把触发器未拒绝等同于内容 exact。

V8→V9 preflight 只接受 pristine，或者只含完全闭合的 claim/pipeline-control 历史。任何 effect/result/proof、非 claim worker operation、孤儿保留 receipt、ledger 断链、未被 head 包含的 suffix、attempt 或 current closure 漂移都会在任何 V9 DDL 留下前拒绝，整笔回到原样。已挂载的 V8 authority 可以在 exact V8 checksum/fingerprint/root 与 exact V9 tail 同时成立时被 current fresh reader 继续消费；binding 仍保留 V8 target，不伪造 remount或改写历史证据。V7、未知版本或任一 identity 漂移都 fail closed。

3.20.0 的交付边界只有三张 DDL、触发器、升级 preflight 与 mount-prefix 兼容；当时“V9 有表和门禁”确实不等于“payload 已可写入并独立审计”。3.21.0 只在这个冻结 schema 上追加窄 writer/auditor，没有修改任一 migration identity，也没有把它扩大为 dispatcher、一般 worker transition、cutover coordinator 或 authority router。

### 2.25 3.21.0 OUTBOX sidecar writer 与 plan-free durable audit

`SQLiteWorkerProjectionWriter` 只接收已由纯 integrity issuer 完整封闭、彼此对应的 `WorkerExecutionPlan + WorkerResultProjectionPlan`，且操作必须是 OUTBOX `AUTHORIZE_EFFECT / COMPLETE / RECONCILE`。writer 在一个 `BEGIN IMMEDIATE` 内重证 mount、authority、target before、shared ledger 与 projection pair，按固定 SQL 写 generic/specialised receipt、ledger entry、V8 effect/result/reconcile mirror、V9 effect/result/proof sidecar，再精确 CAS `event_outbox`、`worker_target_current` 和必要的 dispatch head，最后才推进 worker ledger head。列级 authorizer、pre-COMMIT closure、COMMIT 后 fresh reconcile 与 storage durability barrier 任一不成立，都不得报告 committed。`EXPIRE` 不能进入该 writer，也不能伪造 result/proof。

`SQLiteWorkerDurableAuditor` 不接收原 `WorkerExecutionPlan`；输入只有 request id 和可选的最小 `WorkerDurableIdentity`。它在一个 pinned read snapshot 中对整条 shared ledger 做有界扫描：OUTBOX claim/authorize/complete/reconcile 从 canonical worker manifest、V9 raw payload sidecar 与耐久 target 重建纯 request/plan/projection，重算 receipt/tail/closure，并闭合 allocation chain/head、per-target before→after、`worker_target_current.previous_state_sha256`、legacy event 全部可变列、V8 mirror、V9 sidecar、dispatch state/previous/head/time 与 worker head predecessor。3.23.0 又加入 multi-allocation commit-batch 重放，并从 persisted intent/receipt/legacy transition/target 重建 pipeline CREATE/TRANSITION control，逐项闭合 generic/specialised receipt、atomic member、ledger 与 current；无 worker progress 的 action/pipeline genesis也进入审计根。返回前再开第二个 fresh snapshot 核对 runtime 与 ledger publish fence，不跨 snapshot 拼证据；正向 `EXACT / ABSENT` 还要经过统一 storage durability barrier，屏障失败只能 UNKNOWN。

查询只有 `EXACT / ABSENT / CONFLICT / CORRUPT / UNKNOWN` 五种结果。摘要、mirror、所有权或链闭包被反证才是 CORRUPT；支持面之外的合法 renew/expire/retry、ACTION/PIPELINE worker execution、当地 entry/byte 预算超限、BUSY/I/O 或 publish fence 变化都是 UNKNOWN。multi-allocation 已支持，但 atomic-v1 仍不支持单 operation 扇出多 event，二者不能混为一谈。这个区分让旧 claim 在后缀全属受支持操作时能用 expected identity 恢复 exact，遇到未支持后缀时仍 fail closed，不把“还不会读”误诊为“数据已损坏”。

### 2.26 3.22.0 schema-v10 时间与 dispatch 元组硬化

`runtime_timestamp_dispatch_hardening_v1` 是 current schema-v10 的唯一追加迁移。它把 V8/V9 作为永久冻结的历史前缀，不修改两版的 DDL、migration checksum、schema fingerprint、ledger root或25+3表，也不创建新的业务表。V10 自身 identity 固定为 migration checksum `1be88f2630f37ca45d1e84ea7547b78ebc9da48385774d7e279b74721af81baa`、schema fingerprint `4ef1cb22745c3982b00077762a741371adbc17ca057bf908eb18a1642a45bc5e`、cumulative migration-ledger root `f0ebef5cc0970ae693396769326e06ac651cf96702ce5c426571464bb32392d7`。

V8/V9 有 32 个时间列原先依靠形如 `length(...)=24 AND strftime(...)=value` 的 CHECK。SQLite 把 NULL CHECK 结果视为通过，所以一个长度恰好 24、但 `strftime` 无法解析的字符串可能穿过旧 DDL。V10 不重写旧表，而是安装 34 个 timestamp trigger：append-only 表在 INSERT 时检查，可变 head/current 在 INSERT 与 UPDATE 时检查，生产保留 transaction receipt 父行也只在属于保留 prefix/kind 时进入同一规则。required 列必须是 TEXT、exact 24-byte、可解析且格式化后逐字相同的毫秒 UTC；nullable 列只允许真 NULL 或同样 canonical 的时间。错误统一为 `production_timestamp_invalid`，不会把“长度正确”误当成“时间有效”。

`outbox_dispatch_head` 另有 INSERT/UPDATE 两个 tuple trigger。published 合法形状只有两种：`published_sequence=0` 且 revision/ordinal 都为 NULL，或 sequence 为正且 revision/ordinal 同时存在；inflight 则必须整组为 NULL，或者 sequence/revision/ordinal 全部存在且 `inflight_sequence = published_sequence + 1`。任何正 published sequence 配半个 pointer、或 inflight 只填部分列，都会以 `production_outbox_dispatch_tuple_invalid` 拒绝。

V9→V10 preflight 会先扫描所有受保护时间、生产保留 receipt 父行与 dispatch 元组，再安装 guard。已有 24-byte 不可解析时间或部分 tuple 的 V9 不会被自动修复，而是整笔 migration 失败：`user_version`、migration ledger、metadata、旧数据与 schema 都保持 V9 原样，也不会残留 preflight 临时表或任一 V10 trigger。健康 authentic V9 才能升级；旧 V9 writer/reader 打开 V10 必须在 callback 或业务 SQL 前 fail closed，回退只能恢复升级前备份。

这次 hardening 本身只让数据库更准确地拒绝坏写入，不产生任何业务事实。随后 3.23.0 在不改 V10 schema 的前提下扩展了应用层 writer/reader/auditor；dispatcher、renew/expire/retry、pipeline/action worker、cutover coordinator、authority router、server/bootstrap/CommitCoordinator/PipelineSupervisor caller依然缺失。

### 2.27 3.23.0 通用 production write、rooted read 与 durable audit

production commit 的唯一批语义是：target 按 `pipeline → action → outbox` 分组，组内按 source ordinal；allocation sequence 逐 event 前进，同 commit 必须连续、共享 repository revision/时间且 ordinal=`0..N-1`，allocation head state revision 只按 commit batch 前进一次。writer、rooted reader 与 durable auditor 都必须重算这套顺序和 hash closure，不能用数据库自己存的 count/manifest 为自己作证。atomic-v1 仍要求 operation 与 event 一一对应；单 operation 扇出多 event 属于未来 atomic-v2，不得在 SQL adapter 里偷偷放宽。

`SQLiteProductionReader` 不接受任何 `ProductionAtomicWritePlan`。它在同一 `BEGIN` snapshot 中调用 historical rooted projection，然后从 raw row/bytes 重建 inner/outer receipt、三组 target、pipeline control intent/receipt/member、shared ledger、current 与 allocation batch；确认阶段必须同时 fencing runtime、worker ledger和 dispatch head。只有 active head、全部 closure exact 且 scheduler 仍停在 commit 时状态才返回 detached `SQLiteProductionHead(exact_at_commit=True)`。合法 worker 后继是 `sqlite_production_read_progress_unsupported`，READ_ONLY authority 是 `sqlite_production_read_authority_inactive`，历史 commit 没有版本化 snapshot 时是 head-stale，锁/I/O/打开失败是 unavailable；这些都不能被标成数据损坏。无 receipt 支撑的 current 篡改、伪 descendant allocation、outbox 归属/幂等策略重绑定或任一 payload/root 变化必须是 corrupt。

`SQLiteWorkerDurableAuditor` 的全局重放现在理解 multi-allocation batch、pipeline CREATE/TRANSITION 与 action/pipeline genesis。control 必须从 persisted intent/receipt/legacy transition/target 重建纯 request，再由 integrity issuer 重发，逐项比对 manifest、generic/specialised receipt、atomic member、ledger 和 current；action mirror 必须包含完整 genesis 可变态。raw business manifest先做 bounded canonical decode并按 baseline/change domain重算摘要，worker-relevant groups再与耐久行逐项闭合；outer的八个 hash domain、mount、scheduler历史端点、allocation before/after、targets及最终receipt全部重建，不能用存量摘要互相作证。ACTION/PIPELINE一旦出现尚未冻结的 worker execution，或其他合法但未支持的 operation，仍返回 UNKNOWN；真正的 hash/identity/数量/状态闭包反证才是 CORRUPT。V10对zero-control历史commit仍缺独立的提交时scheduler checkpoint：auditor只能证明其引用真实完整endpoint，不能证明as-of唯一；当前runtime control revision若超出mount可证明端点也必须UNKNOWN。长期authenticated checkpoint/全局事务序是协议阻断项，不得在文档中冒充已完成。

### 2.28 3.24.0 durable source freeze、canonical stage member 与 schema-v11 cutover closure

文件 source freeze 是独立于 admission JSON 的耐久前驱，不是“锁还握在内存里”的别名。唯一 installer 必须已经持有严格 catalog EX → repository EX 迁移租约，当前 admission 必须是 exact legacy，且完整 terminal repository snapshot 已在同一租约内重证。随后写入 owner-only、no-follow、单硬链接、4 KiB 上限的 `.repository-cutover-freeze-v1.json`；canonical identity只含 authority epoch、source admission revision、runtime generation与code digest，`freeze_sha256`由独立domain计算。写后必须重新读取比对；确认丢失若fresh read看到exact marker只能报告durability unknown，再由同identity exact retry收敛，不能删除marker重来。

freeze一旦存在，普通repository/project/Document生命周期、读取、复制、catalog scan与SH activity lease都必须在callback和业务I/O前以`legacy_frozen`拒绝。严格migration recovery租约仍可读取exact source，目的是继续向前完成cutover，不是重新开放legacy writer。每次`legacy → cutover_pending → sqlite` admission CAS先核对marker与source revision；最终SQLite authority lease还必须核对runtime generation/code digest。缺marker、marker损坏、identity不一致或pending/sqlite admission没有对应freeze都是recovery-required，不能按普通“尚未开始”处理。freeze没有自动删除、回退或解冻协议；进入该边界后只能forward-repair。

asset stage的SQL member projection是coverage carrier到`business_asset_stage_members`的唯一纯映射。project、blob、occurrence分别定义稳定identity：project由project id+generation标识，blob由content SHA标识，occurrence由project/generation/entity/path/deleted/content标识；SQL `member_id`由bounded canonical identity manifest和kind-specific domain得到。`member_sha256`绑定kind内稠密ordinal、member bytes、metadata digest与完整coverage value；`metadata_sha256`绑定完整source metadata。三种member kind与identity/value/metadata三种用途均分域，project的仓库ordinal留在value hash中但SQL ordinal重新按kind稠密化。project bytes固定0，blob/occurrence bytes分别对应unique/referenced closure。StagePlan冻结全部projection，require会重建顺序、预算与每一摘要；writer只能逐字段写它，reader只能从耐久coverage重建它再比较，不能把schema测试里的任意占位摘要当算法。

耐久cutover receipt的唯一时间偏序是：

```text
durable source freeze → asset repository coverage ≤ read-only install
                                             ≤ audit ≤ activation ≤ mount
```

freeze必须先于任何cutover shadow写入，使后续stage/coverage看到不可再被普通legacy入口推进的source。coverage又必须在install前完成，因为它证明这份已冻结source的全部asset closure；install只有在source已被fence且coverage已耐久后才可成立。audit读取install及coverage之后的同一registry事实，activation消费fresh audit与final file admission，mount最后组合全部前驱。mount-v2 contract、issuer与V11 table CHECK共同拒绝反序；所有时间仍是exact 24-byte canonical毫秒UTC。head的阶段名仍为`installed → assets_covered → audited → activated → mounted`：phase 1先建立read-only install head，phase 2在同一install时点把已经耐久的coverage pointer纳入head。因此head展示顺序不等于重新提交receipt，不允许据此把install时间写到coverage之前。

`business_cutover_head_integrity.py` 是五阶段head的唯一hash定义。每个state都绑定cutover/repository/runtime/code identity、`state_revision = phase_revision = 1..5`、phase、authority state、从install开始按角色累加的typed receipt pointer、前一head与updated time；前三阶段只能read-only，activation/mounted只能active。第一项以前驱零摘要开始，后项逐一链接，manifest受1 MiB/10,000 nodes预算限制并在`ze.business-cutover.phase-head.v1` domain下摘要。V11 table的NULL存在矩阵、FK、initial/next-phase trigger、旧pointer不变规则和generic head CAS共同限制SQL形状；fresh production authority reader还会从最终mount evidence重建全部五态，并把最终state的每一列与row逐字比较。数据库里已有一个看似合法的`head_sha256`不再足够。

schema-v11 migration名为`mount_chronology_state_closure_v1`。V1–V10 DDL/checksum是不可改写前缀；V11自身checksum=`48de0b2513175f8575a7127e54f684922db4eb687739e0ebbab93a0a524f54cb`，schema fingerprint=`d92e9a6ac8c58ef6ed598a4c53bc972239f93b8e8c2a4d8a7e54369e3dc1c74f`，cumulative migration root=`39c8bc8f749de98b64d7a89b27a2d8496f46f39e1edb3e5ff0be83599314de70`。V10→V11 preflight必须同时证明exact十条ledger、legacy authority、runtime/business transaction表为空、28张production/sidecar表为空、所有production reserved receipt为空；fresh全registry初始化也只能从全空状态建立。preflight在任何drop/create前失败并完整回滚，不留下临时表或半个V11。

V11会重建两张仍为空的V8表以收紧CHECK/trigger，而不会迁移其中的数据。这是刻意的安全边界：已有install、stage、coverage、audit、activation、mount、worker、outbox或active runtime的V10都不允许升级。尤其是历史mounted V8/V9/V10数据库，V11**不承诺原地升级、自动重签head、重排时间或把旧target冒充V11 mount**。真正需要保留这类历史实例时，必须另立经过审计的export/rebuild、平行新库切换或backup restore协议；清空production表、手改`user_version`/ledger或同步重签所有摘要都不是升级。

### 2.29 3.25.0 baseline projection、incremental head 与 V11 persistability blockers

`business_cutover_baseline_projection.py` 是install期baseline的唯一纯SQL-row投影。它先重验sealed `BusinessCutoverPlan`和每份`BusinessSnapshotSet`，再按V11真实列序生成live项目的project/document/entity、generic receipt、baseline business commit、repository/head current、commit-scoped asset occurrence，deleted项目的generation high-water，以及全仓去重asset blob和worker/allocation/dispatch genesis head。repository revision只给live baseline按1..N连续编号；每个project/head revision固定从1起。trace id、空before root、空operation tail、timestamp、asset content-key、optional metadata合并、同SHA冲突、row/byte预算与projection manifest都只有一套规则。projection不读取provider字节；未来writer只有在同一cutover的fresh exact coverage证明blob长度/metadata后才能把预期`available`行写入。

五阶段head不再只能从最终mount一次生成。phase 1 initial issuer只消费install pointer；每个advance只接受exact canonical前驱和唯一下一角色pointer，重新构建完整前缀并核对cutover/repository/runtime/code identity、previous hash与`coverage ≤ install ≤ audit ≤ activation ≤ mount`。按五次增量得到的manifest/hash/row必须与完整chain逐值相同。exact前驱重试确定性幂等，但对已经前进的head重放旧pointer、跳级、倒退、终态再推进或同步重签假前驱一律拒绝。

`business_cutover_persistability.py` 只做V11写I/O前的carrier preflight，不签receipt或authority。stage、coverage、audit、activation、mount与五个phase-head先各自重验sealed plan/chain，再按`MAX_BUSINESS_SMALL_PAYLOAD_BYTES=16MiB`量每一个真实manifest BLOB；失败只暴露稳定phase/carrier/actual/limit，不回显payload或repository identity。install刻意不返回persistable：V11 install row只有`member_mapping_sha256`，没有mapping bytes，也没有能从耐久rows完整重建operation/source/import/baseline全部字段的冻结合同。

baseline的第二个阻断同样不能靠时间造假消除。baseline commit在install事务中发生，所以合法chronology是`baseline/install ≤ mount`；冻结的production-atomic outer协议要求`mounted_at ≤ committed_at`，只适用于mount后的普通commit。V11 projection因此只生成zero-control inner atomic seed，并固定`outer_atomic_status=requires_schema_upgrade`。真实正向chronology返回明确schema-upgrade requirement，反向mount时间判corrupt。下一schema/protocol必须提供baseline-specific outer closure并让read-only install audit完整闭合；禁止先写business commit、mount后再追认，或给install行填未来时间。

从schema-v8引入outer atomic table开始，full business audit同时增加business commit→atomic receipt的反向anti-join；冻结历史V7没有该表，仍使用其原closure。已有FK只证明atomic row引用真实commit，不能发现离线篡改者单删atomic row；V8+现在即使其他revision/current projection仍连续，也会以`sqlite_business_atomic_receipt_missing`停止。这个反向规则与上述pre-mount blocker共同意味着：V11不能被直接拼成production coordinator，必须先正面升级schema/protocol。

### 2.30 3.26.0 schema-v12 install closure carrier 与 fresh semantic read

schema-v12 migration 名为 `cutover_install_closure_v1`，checksum=`aa61960d2bd4e10a41d1ef51767bf4f4809f5fab26cae5ebef3bfa30fccf35a2`、schema fingerprint=`4c265275dcf7d90e4ee77fda3b3a1749646e5e55783ce17fdff40e737195b226`、cumulative migration root=`1537102bc6fe371ceef49a58b566dbd6f86940eaf838dcfeb263e3df37cbb77e`。V1–V11 的 registry、DDL、checksum 和历史行语义全部冻结；V11→V12 preflight 只接受 authentic V11、legacy authority、原有 production namespace 全空，且既存 V5 source receipt 唯一并使用规范 UTC 的数据库。V12 不重建旧表，只追加 `business_cutover_install_artifacts`、`business_cutover_install_artifact_chunks`、`business_cutover_baseline_atomic_receipts`、`business_cutover_install_closures` 4 张 `STRICT` 表。因此 V12 当时的 production/runtime 表数是 `32 = 28 张历史 V8/V9 表 + 4 张 V12 表`，且 V12 自身没有额外 4 张阶段 binding 表；current V13 已在不改写这段历史的前提下新增那 4 张表，当前总数是 36。

install artifact 只有 `member_mapping` 和 `baseline_projection` 两类。每个 artifact 的 canonical bytes 被切成 ordinal/offset 稠密的块；单块上限 16 MiB，最多 4 块，因而单 artifact 总上限是 64 MiB。非末块必须满块，头行声明 byte length、chunk count 和 artifact SHA-256；关系约束只能闭合块数、offset 和总字节数，artifact SHA-256 仍须由 semantic reader 对重组 bytes 重算。closure 形成后，append-only/closed guard 禁止再补 artifact、chunk 或 baseline sidecar。这样 member mapping 不再只有一个无法反查原文的摘要，read-only install 可以从耐久 bytes 重建映射。

`business_cutover_baseline_atomic_receipts` 为每个 live baseline commit 保存一条 baseline-specific outer sidecar。它先逐列镜像已有 `atomic_business_write_receipts` 的 inner atomic 事实，再绑定 install parent、两份 artifact 与 baseline outer manifest/receipt；baseline 与 install 使用同一 canonical `committed_at`，不引用尚未发生的 mount。baseline 不产生 worker target，所以 `target_count=0`；`targets_sha256` 必须调用既有 production target 规范，对 pipeline/action/outbox 三个空组计算，不能用零摘要或新造的“空 targets”常量。

baseline projection 升为 v2 只为补齐上述 durable carrier 与 outer status。asset blob 仍以 `available` revision 1 投影，因为它表示的是已由 repository coverage 确认的 baseline 事实；不允许先写虚构 `staged` revision 1，再写 `available` revision 2 来凑状态机。未来 writer 在写入任何 `available` blob 前仍必须重证同一 cutover 的 fresh exact coverage；V12 的 install closure 不替代 provider 或 coverage 证明。

`business_cutover_install_closures` 把两份 artifact、live/deleted/project counts、baseline sidecar count、连续 repository revision 和 baseline outer receipt root 绑到 install parent，并增加十个不可含糊的来源指针：V5 source 的 transaction/kind/revision/manifest/proof/admission 六项，以及 V8 coverage 的 transaction/kind/manifest/coverage 四项。复合 FK 和 closure trigger 要求这些指针同一 cutover、epoch、runtime generation、code digest、项目计数和 `source ≤ coverage ≤ install` 时间线；V5 source generic receipt 从首次 INSERT 就必须是 canonical UTC，kind/prefix/时间随后永久禁止 UPDATE/DELETE，V11 中已存在的不规范父回执会在任何 V12 DDL 前整笔拒绝。initial cutover head 只能在 exact closure 之后创建；closure 到 mounted 之间的 36 张物化表保持冻结，只有 mounted head 与 exact active runtime 同时成立才解冻。

这些 FK/CHECK/trigger 能拒绝缺块、多块、chunk ordinal/offset 错乱、断 revision、数量错配和 closure 后追加 install 成员等关系错误，但 SQLite 本身不会理解 canonical manifest 或重算业务 hash。`business_cutover_stored_manifest.py` 是 install/coverage 两个 SQLite reader 共用的唯一 bounded parser；`sqlite_cutover_install_provenance_reader.py` 会从 raw V5 seal/control/member 重新生成 repository proof，再逐批解码 V8 stage、重算每个 stage member 和全仓 project/blob/occurrence/coverage root，最后只签发 issuer-stamped provenance binding。`sqlite_cutover_install_closure_reader.py` 必须消费该 binding，再从原始 chunk、inner mirror、baseline outer 和 closure 行重组纯投影，重算 V12 chunk/artifact、baseline outer manifest/receipt、baseline receipt root 与 closure manifest，并把可能因 deleted member 留空洞的 live ordinal 逐项对回 member mapping。关系正确但任一 V5/V8/V12 hash、清单形状或来源时间错误的库不得授权。

空 install 没有 baseline commit，但 runtime revision 1 是明确的 bootstrap high-water；mounted 后首个普通 baseline 因而必须是 global revision 2，后续连续增长。非空 install 仍从 revision 1 连续。full audit 只能从 exact closure 的 `baseline_commit_count` 推导这一处合法偏移，不能从观察到的缺口猜测；同一审计在 phase 1/2 的 read-only install 阶段也必须可运行，不能反过来要求尚未发生的 mount。mounted 后 reader 对 install baseline 使用 immutable historical rows，而不是要求当前 projection 永远停在安装时刻。

上述 fresh read 是启动/授权级的全证明，不是每笔提交的热路径：它可能解码接近 artifact 上限的 carrier 并遍历全部来源、stage 和 baseline 成员。生产挂载前必须把它放在 startup/full-audit 边界，并以后续 authenticated checkpoint 或小型 immutable hot fence 承担事务级准入，不能让 N 级全扫变成每次编辑的延迟。

3.26.0交付当时还没有install writer和plan-free auditor；这是历史边界。3.27.0增加install writer，3.28.0增加durable auditor，3.29.0增加phase-1 finalizer，3.30.0再增加V13四类direct-predecessor carrier、guard与fresh semantic reader。后续阶段writer、durable coordinator、authority router、性能checkpoint与production caller仍未接好；current V13仍是private/unmounted substrate，真实业务保存继续由`FileCommitStore`承担。

### 2.31 3.27.0 schema-v12 install fixed-SQL writer 与生产零边界

3.27.0不新增migration，也不改V12的表、索引、trigger、checksum、schema fingerprint或cumulative migration root。新增private install writer只接受经`_require_business_cutover_install_closure`完整重验的projection与模块私有capability；它不接受raw cutover plan、baseline、coverage、调用方clock、裸connection、SQL callback或可替换authorizer。projection仍是唯一列值来源，writer不能从摘要反推或补造业务字段。

真实non-empty terminal shadow在V3 importer阶段已经写入`projects / documents / entities`。install writer必须在同一个snapshot中把这三表当作既有source事实逐行exact只读验证，禁止对其执行INSERT、UPDATE或DELETE；fixed-DML和authorizer白名单不得把三表列为writer-owned。列级写入形状由fixed SQL冻结每条INSERT/UPDATE的目标列和值位次，并不是authorizer单独提供列级保证；authorizer另行限定`main`数据库、无trigger来源、固定INSERT表和唯一可更新的`kernel_repository.authority`列，同时拒绝DELETE、DDL、PRAGMA、ATTACH与事务逃逸。install事务只负责此前尚未耐久发生的generic/install receipt、**数据库内部**`kernel_repository.authority: legacy → sqlite` CAS、read-only runtime及三组genesis head、V12 artifact/chunk、install-time baseline/outer sidecar与closure。这个marker只说明SQLite内部安装态，不等于文件admission CAS或production authority切换；`FileCommitStore`仍是线上事实源。writer会写install-owned的`asset_blobs`与`document_asset_refs`物化行：identity取自baseline，MIME、extension和byte length由同一fresh coverage descriptor补齐；它不写provider、stage或coverage事实。测试fixture清表、关FK或重复写入V3业务行不能替代这条真实边界。

唯一写事务是一个`BEGIN IMMEDIATE`。首条DML之前，writer先重验projection、current schema、V3 terminal shadow、V5 source/V8 coverage identity。V3 import、V4 catalog、V5 repository source三类legacy generic receipt必须与各自STRICT child一一对应，查询prefix/kind时显式`CAST AS TEXT`，所以TEXT事实旁边再塞byte-identical BLOB也会被识别为污染。production-reserved generic receipt同样使用CAST-aware精确补集：只允许projection已盖章且有exact child的stage与coverage前驱，任何install、join、business commit、worker、pipeline、audit、activation、mount孤儿或prefix/kind碰撞都必须停止；完整但与请求完全不同的唯一前驱是`CONFLICT`，expected前驱仍在却又混入extra则是`CORRUPT`，普通非生产kernel receipt原样保留。install时间不能来自调用方clock，projection内的`committed_at`会在首写前和COMMIT前两次与同一SQLite连接的毫秒UTC时钟比较，未来时间按冲突拒绝。通过这些前置后才按固定SQL写每个owned surface；`business_cutover_install_closures`必须是最后一条业务DML。closure之后连续两次调用既有fresh semantic reader，在同一事务snapshot内重组V5 source、V8 coverage、V12 chunks/artifacts、baseline inner/outer与closure root；两次结果必须完全相同，并且第二次读取前后不得再执行写SQL。COMMIT返回后仍须用新连接做fresh exact reconcile，再通过唯一storage durability barrier才可向调用方确认。

pre-COMMIT普通失败只有在全部writer-owned surface仍为空、V3 source三表保持原样时才能分类为`NOT_INSTALLED / cutover_install_rolled_back`；exact retry和COMMIT acknowledgement loss可由fresh read恢复同一结果。preinstall V3 terminal shadow或其他直接前驱已经与projection分叉、请求时间在未来、同一cutover已有完整安装但projection不同，分类为`CONFLICT`；SQLite-owned namespace只出现一部分、production-reserved receipt补集被污染、foreign request试图把既有partial SQLite runtime降级、或已安装runtime/closure/耐久时间自身损坏，分类为`CORRUPT`。打开、锁竞争、I/O、closure读取或durability barrier不可用统一为`UNKNOWN`；reconcile若无法完成rollback+close也只能发布`UNKNOWN`，进程控制异常则在best-effort清理后原样传播。这里按“已存在但不同的完整事实”“自身不闭合的耐久事实”和“当前无法取得可靠观察”分域分类，禁止自动重放。writer不拥有file admission、maintenance/EX lease、asset provider/stage/coverage写入、全局plan-free cutover auditor、audit/activation/mount、active flip、dispatcher、coordinator、authority router或任何server/bootstrap caller。

### 2.32 3.28.0 schema-v12 plan-free cutover durable auditor

3.28.0不新增migration，也不改变V1–V12任何DDL、checksum、schema fingerprint或cumulative migration root。新增private `SQLiteCutoverDurableAuditor`只接受exact `SqliteDatabase`；`inspect()`除可选detached expected identity外不接cutover plan/projection、调用方clock、裸connection、SQL callback或writer capability。auditor及其test support均不进稳定SDK，没有server/bootstrap/production caller。调用方在未来接入时仍必须持有整个调用期间的external repository/maintenance lease；pinned WAL snapshot、inode抽样和fresh fence只能检测普通替换竞争，不能抵御无视租约的恶意A→B→A。

状态空间有六个可恢复点。install writer以V12 closure作为事务最后一条DML，因此成功后`business_cutover_head`仍为空是合法的`INSTALLED_CLOSURE`，不是损坏；首次`INSTALLED_HEAD`必须由后续独立、可恢复的phase-1事务创建。之后依次是`ASSETS_COVERED / AUDITED / ACTIVATED / MOUNTED`。auditor在同一个pinned read transaction内重证current schema/security/FK/full audit、legacy与production reserved receipt、V5/V8/V12 install closure、baseline项目/文档/实体/资产/scheduler全量覆盖、audit certificate链、activation、mount与逐步重放的canonical head。phase 0–3 runtime时间必须等于install时间，phase 4–5在无post-install suffix时必须等于activation时间；audit、activation、mount都不得晚于该snapshot开头取得的同一SQLite-now上界。

发布只允许五态：`ABSENT`表示pristine current V13且完整证明没有install事实；`EXACT`表示数据库先自证的identity与可选expected相同；`CONFLICT`表示数据库自身完整但与expected不同；`CORRUPT`表示稳定snapshot内存在不可闭合事实；`UNKNOWN`表示当前无法可靠判断或形状尚未支持。V13审计会把四张binding manifest及其canonical head链纳入自证与二次publication fence；expected永远在数据库自证之后比较，不能用foreign expected把partial库降级。

稳定坏库必须先于“不支持”被发现。production receipt逐行要求canonical UTC及kind↔prefix↔suffix；mounted且没有普通后缀时，closure reader被强制重证当前项目/资产/scheduler仍与install snapshot逐项一致，不能因mount进入historical分支而漏过额外row或head漂移。首张INSTALL audit certificate的receipt count/root由已知因果后继集合精确排除后重建。后续certificate在预算内只重证21个raw列、generic parent、sequence、previous id/manifest/tail、纯`repository_audit_integrity`前驱单调约束、worker `(sequence, tail)`在当前append-only ledger中的membership与最终audit head；其runtime revision/time、receipt count及worker sequence还必须不超过当前append-only高水位。它不证明successor receipt root或worker as-of high-water，也绝不按毫秒时间戳猜全局顺序。只有整链及activation/head/mount/runtime都健康、且没有普通post-mount suffix时，才以`certificate_suffix_unsupported`返回UNKNOWN。pre-mount business/worker suffix是CORRUPT；合法mounted普通suffix则在完成最低mount/full-audit结构检查后返回`post_mount_suffix_unsupported / UNKNOWN`，与periodic证书并存时也以这个更宽的普通后缀限制为准，等待authenticated checkpoint和统一全局次序。

coverage-only空资产、stage-only以及stage+coverage都是合法preinstall中间态，在缺少plan-free provider binding与V12 mapping carrier时不能冒充ABSENT或EXACT；auditor会先验证marker时间、V5 legacy/repository proof、page/FK、reserved receipt一一对应与唯一cutover。coverage存在时还必须证明其batch count与stage数量/identity一致；stage-only则必须至少有一张同cutover stage。混合cutover或数量矛盾归CORRUPT，其余返回`preinstall_unproven / UNKNOWN`。detached audit identity只是恢复决策输入，不持有issuer stamp或任何写权限；3.29.0 phase-1 finalizer因此在自己的`BEGIN IMMEDIATE`中重证raw直接前驱，而不消费auditor结果作为写能力。asset、audit、activation、mount writer仍必须以同样原则签发本事务专属binding。durable coordinator、authority router、production Context、dispatcher、真实多进程/crash恢复矩阵与长期checkpoint仍是后续，不能从auditor green推导“已经切库”。

### 2.33 3.29.0 schema-v12 phase-1 installed-head finalizer

3.29.0不新增migration，也不改V1–V12任何DDL、checksum、schema fingerprint、cumulative migration root或表数。新增private `SQLiteCutoverPhaseHeadWriter`只负责把已经耐久、但尚无`business_cutover_head`的`INSTALLED_CLOSURE`收口为`INSTALLED_HEAD`。构造器只接exact `SqliteDatabase`与模块私有`_CUTOVER_PHASE_HEAD_WRITER_CAPABILITY`；`finalize()`不接cutover plan/projection、调用方clock、裸connection、SQL callback或detached auditor identity。`SQLiteCutoverDurableAuditor` 的输出仍是诊断证据，不是finalizer写权；写适配器必须在自己的事务中从数据库重证。

唯一写事务是一个`BEGIN IMMEDIATE`。BEGIN之后立即安装transaction-wide mutation firewall：schema/security与前后两次raw proof只允许SELECT和精确read-only PRAGMA，只有canonical head INSERT的极窄窗口临时开放写权；proof helper、test hook或trigger尝试其他DML都会被authorizer拒绝并整笔回滚。首条也是唯一一条DML之前，finalizer从raw rows重证install generic receipt与`business_cutover_install_receipts`的specialised child逐列一致，重证V12 install closure及其canonical install identity，要求`repository_runtime_control` 仍是同一cutover的`authority_state=read_only / state_revision=1`，并证明head与post-install业务、worker、audit、activation、mount后缀为空。它不信任已物化的摘要为自己作证，也不因auditor曾经返回EXACT而跳过这些raw校验。

全部前置精确时，唯一写入是一条fixed `INSERT INTO business_cutover_head`：行必须是纯`business_cutover_head_integrity` issuer能重建的revision-1 `installed / read_only`状态，只含install pointer，previous head为genesis。不允许UPDATE/DELETE、追加receipt/runtime/closure、借trigger代写、跳到phase 2，或把外部传入的head row直接落盘。exact retry必须收敛到已有的同一canonical head；已经前进或结构不闭合时不得用重签摘要覆盖。

COMMIT返回不足以对外确认成功。finalizer必须再用新连接的`reconcile()`重证install receipt/closure、read-only revision-1 runtime、唯一initial head与仍为空的suffix，然后通过kernel唯一storage durability barrier才返回确定成功。pre-COMMIT失败只有在fresh观测能证明零head残留时才是未完成；acknowledgement loss可以由exact fresh head恢复；打开、BUSY/I/O、cleanup、fresh reconcile或durability无法给出稳定观测时不得猜测。

这是database-only的phase-1收口，不是production cutover。模块、capability、result/status/test-point均保持private，不进`sdk.__all__`或public lock，server/bootstrap/CommitCoordinator/PipelineSupervisor/production authority router均没有caller。它不修改file admission、不翻active、不写asset-provider/audit/activation/mount阶段，也不取代外部maintenance lease。因此当前只能说“headless install可以被唯一地收口为phase 1”，不能说“生产authority已切换”。

### 2.34 3.29.1 neutral cutover transaction verification

`sqlite_cutover_verification.py`是auditor与fixed-SQL phase writer下面的只读中性层。它唯一实现全私有表logical-row与TEXT/BLOB预算、SQL VM progress budget、legacy/production reserved receipt逐行闭合、exact行比较及SQLite/kernel/OSError cause-chain的不可用分类；它不导入durable auditor或phase writer，不认识五态、detached identity、writer capability，也不包含任何mutation SQL。auditor只负责把typed verifier failure翻译为原有诊断五态，writer只负责把同一事实校验嵌入自己的事务与写权限边界。由此“事实是否闭合”只有一套算法，“闭合后该怎样读或写”仍由各自owner决定，既消除了复制漂移，也避免writer依赖auditor内部函数形成错误层级。该PATCH不改schema、错误码、状态机或生产挂载范围。

### 2.35 3.30.0 schema-v13 direct-predecessor phase bindings

V13在冻结V1–V12全部identity的前提下追加四张阶段binding表：asset-provider、audit、activation、mount。每张表都不是第二套业务receipt，而是“OLD canonical head + 当前阶段直接证据 + 外部admission/maintenance观察 + runtime/receipt/worker/allocation/dispatch checkpoint + expected NEW canonical head”的不可改写sidecar。四张表分别固定phase 1→2、2→3、3→4、4→5；head更新的反向trigger不仅要求本表逐列绑定OLD/NEW与本阶段NEW pointer，还会重新核对推进瞬间的live runtime、receipt count和三类scheduler checkpoint（audit另核live audit head）。相邻表的forward guard还必须把source admission/freeze、同状态file admission、maintenance proof、上一阶段after receipt root、runtime与scheduler checkpoint逐项传下去；activation→mount额外绑定after-runtime、install和final admission，所以不能把来自不同cutover、不同证据或不同历史快照的四行拼成一条链。DDL保证的是“推进时仍为同一快照”，不是“binding INSERT与head UPDATE必然位于同一事务”；未来唯一fixed-SQL writer仍必须把两步放在同一个`BEGIN IMMEDIATE`中，孤立但未过期的binding本身不授予authority。首个head之后generic receipt保持INSERT-only，防止同count改写root，又不阻断post-mount生产后缀。

纯合同为四阶段分配独立hash domain，canonical manifest递归包含所有显式dataclass字段；issuer只从完整projection产生，verifier会重建exact类型、provider deterministic-JSON receipt和全manifest，后构造篡改、stage/evidence替换或任一checkpoint字段变化都失败。SQLite表仍逐列保存关键事实，writer不能只落manifest hash。`sqlite_cutover_phase_binding_reader.py` 是这四张表的唯一 fresh 语义读取 owner：它要求调用方已持有事务，核对 exact V13 表列与prefix，从原始行重建全部 projection 字段、Audit→Activation→Mount 嵌套前驱、canonical manifest、direct source/generic receipt/current cumulative head，并拒绝缺行、多行、跨阶段拼接和仅结构 JOIN 看似成立的篡改；production authority reader与global durable auditor只消费这一结果，不各造一套解释。外部EX maintenance lease本身不伪装成数据库行：V13只记录lease内观察到的source freeze、file admission和repository proof，活权限仍必须由未来唯一private coordinator在事务前后fresh确认。

V12→V13 preflight只接受authentic、legacy、17+32私有数据面全空且production reserved receipt为空的V12；任何已经开始cutover的V12都在首张永久表之前原子拒绝，因为旧历史没有四类binding，无法被可靠补签。V13 checksum、fingerprint与migration root由`GATE-DB-21`冻结；current私有数据面为17张transaction与36张production/runtime表。3.30.0交付可表示性、纯签发、数据库正反约束与fresh全字段语义reader；3.31.0再交付第一段private asset-provider writer。仍属后续的是audit/activation/mount三个writer与coordinator/router，不能把“binding可证明或private writer可执行”说成“生产authority已切换”。

### 2.36 3.31.0 V13 asset-provider fixed-SQL phase writer

四阶段binding的SQLite行投影由纯`business_cutover_phase_binding_persistability.py`唯一拥有。它先完整重验binding，再按stage返回不可变的table、固定columns和值tuple；asset/audit/activation/mount分别固定91/87/89/99列。它没有SQLite、connection、SQL callback、capability或I/O。semantic reader在重建纯binding后必须再次通过同一mapper投影完整存储行并逐列比较，因此“合同字段已经验证”与“数据库每一列没有额外漂移”成为同一条证明链，fixture和writer也不能维护第二份字段顺序。

`SQLiteCutoverAssetProviderPhaseWriter`只接受exact `SqliteDatabase`、模块私有capability，以及同PID、同线程、仍active且由私有issuer创建的external observation。observation只携带SQLite无法自行发现的当前pending file admission、source freeze/maintenance观察和provider checkpoint；它不是可持久化的lease token，也不具备跨线程/跨进程权限。当前issuer只供私有测试，production caller必须保持为零；未来唯一coordinator要在严格catalog EX→repository EX锁与provider一致性snapshot仍然存活时创建carrier，并让这两个外部上下文覆盖整个`advance()`。

`advance()`不会把事务外durable auditor的READY结果直接当写权。它在自己的唯一`BEGIN IMMEDIATE`内安装transaction-wide authorizer，从raw rows重证current V13 schema、install generic/specialised receipt、V12 closure、coverage、read-only runtime、receipt namespace与worker/allocation/dispatch高水位，并把事务外READY identity的cutover/epoch/runtime/code/install/closure/source/coverage/head/schema/ledger/receipt-root字段逐项绑定到事务内snapshot。随后由纯issuer生成provider evidence和asset binding，只在第一个窄窗口执行91列fixed binding INSERT，再在第二个窄窗口对phase-1 head做exact CAS UPDATE；semantic reader必须在head更新后、COMMIT前读回唯一phase-2前缀。authorizer拒绝其他INSERT/UPDATE、全部DELETE、trigger代写、DDL、写PRAGMA、ATTACH/DETACH与事务/savepoint逃逸。

成功、失败和重试均按耐久事实分类：pre-COMMIT hook、live carrier失效或任一证明失败必须同时回滚binding与head；COMMIT后丢ack只能fresh reconcile并通过storage durability barrier恢复。已有phase-2只能在provider protocol/id/generation/sequence/time/count/bytes/blob root、pending admission及其digest、source freeze、maintenance scope/proof/observed time全部一致时算exact retry，否则是checkpoint conflict。`GATE-COMMIT-15`把这些API、字段、事务顺序、fixed SQL、authorizer、rollback/ack/retry与zero-production-caller变成可执行门禁。它仍不写audit/activation/mount、不翻file admission或active authority，也不替代coordinator/router。

## 3. 唯一合法顺序

```text
acquire project lifecycle activity lease + verify generation
  acquire project commit lock
  validate document id + effective save policy
  session = document.begin_mutation_session()  # optional compatibility port
  with effect_phase(PRODUCER):
    producer()                       # 只允许校验 + Document mutation
      nested execute() => reject     # 必须返回一个显式 CommandMutation
  producer/type error => rollback, re-raise original
  if None/Pending: rollback, return  # 不保存、不记日志、不发事件/不漏内存改动
  CommitStore.commit(intent)
    File store: Document.save
                OpLog.append(op 1..n)
  if Document=NOT_DURABLE and OpLog=NOT_DURABLE:
    rollback; rebuild facts.rollback_succeeded=true; status=aborted
  else:
    session.commit                   # UNKNOWN/DURABLE 绝不恢复旧快照
  if not committed + double durable: return without events
  build canonical events from exact post-commit snapshot
  publisher accepts every event      # 仍在锁内，保证版本/事件顺序一致
  run deferred actions               # 只在 durable + event accepted 后；失败单独记账
  release commit lock
release lifecycle lease
```

producer phase 使用 `ContextVar` 而非 thread-local：async task/线程上下文不会互相覆盖，嵌套 phase 会按 token 精确恢复。框架拥有的外部 effect port 必须调用 `assert_external_effect_allowed()`；命中 producer 时固定抛 `producer_effect_forbidden`。它不能魔法拦住任意第三方 Python I/O，因此还必须配套静态 producer-effect gate。

publisher acceptance 也必须持锁。否则 A 已落版本 2 后解锁，B 可能先落版本 3 并先发事件，造成 SSE 顺序与 canonical version 倒置。W08 引入 durable outbox tailer 后可以把异步投递移出 mutation lock，但 outbox 入队本身仍须和业务提交同事务、同顺序。

## 4. FileCommitStore + MutationSession 失败矩阵

| 事实 | status | 自动重放整条命令 | 发布事件 |
|---|---|---:|---:|
| Document 与全部 OpLog 成功 | committed | 否 | 允许 |
| Document/OpLog 明确未写，session 回滚成功 | aborted | 是 | 否 |
| Document/OpLog 明确未写，但 Document 无 session | dirty | 否 | 否 |
| Document/OpLog 明确未写，但回滚失败 | 抛 `mutation_rollback_failed` | 否 | 否 |
| Document 写入结果未知 | indeterminate | 否 | 否 |
| Document 成功，首条 OpLog 明确未写 | indeterminate | 否 | 否 |
| Document 成功，OpLog 结果未知 | indeterminate | 否 | 否 |
| batch 已追加部分 Operation，后续失败 | indeterminate | 否 | 否 |
| 最后一条 OpLog 明确已耐久但 ack 丢失 | committed | 否 | 允许 |

普通 Python/OS 异常默认是 `unknown`。只有存储适配器能证明结果时，才可抛 `WriteOutcomeError(outcome=...)`。任何时候都不能看到“异常”就把整条业务命令自动执行第二遍。

回滚判据必须同时满足 Document 与 OpLog 都是 `NOT_DURABLE`。只要任一为 `UNKNOWN` 或 `DURABLE`，session 必须 commit 当前内存图并进入修复/对账；此时盲目恢复快照可能让进程内 Document 比已经写盘的 canonical 版本更旧。事件构建、事件投递和 after-commit action 都发生在 session commit 之后，失败绝不触发业务回滚。

## 5. 事件语义

默认 `CanonicalEventFactory` 复用现有 `entity_change` payload，并增加：

- `commit_id`：把一个或多个 Operation 关联为同一提交；
- `document_version`：该事件对应的 canonical 文档版本；
- receipt 的 `trace_id`：跨 surface 使用同一逻辑 trace。

publisher 对 batch 必须尝试接受全部 payload：

- 全部同步成功：`event_status=published`；
- 至少一条被耐久队列接受为 pending，且没有失败：全部接受完成后返回 `pending`；
- 任意一条明确失败：业务仍是 `committed`，事件状态为 `failed`，后续由事件修复流程处理。

事件失败的 `retry_safe=false` 指“不能重放业务命令”，不禁止事件投递器按 commit id 做幂等补发。

## 6. 调用方红线

1. producer 不能把 OpLog 或 broker 传给旧 CommandRegistry；它只能产生 mutation 结果。
2. surface 不能在 coordinator 返回后再次保存、追加或广播。
3. pending/no-op 不能为了“统一返回形状”制造空 Operation 或伪 receipt。
4. `indeterminate` 不能映射成普通 500 后自动重试；必须进入对账/修复状态。
5. 文件 store 不得声称跨文件原子；W06 数据库 store 才能把业务行、操作行和 outbox 放进同一事务。
6. producer 内不得启动/取消线程、写运行态文件或广播状态；这些副作用必须登记为 `DeferredAction`。
7. producer 内不得递归 `execute`；主命令和占位等派生写必须用 `CommandMutation` 合成一笔 receipt。
8. 不得依据异常类型或 message 猜测“磁盘肯定没写”；只有 receipt 的两项 `NOT_DURABLE` 事实允许 rollback。
9. 事件失败、action 失败与任何 `UNKNOWN` 结果不得 rollback；它们只能进入幂等补发或修复。
10. lifecycle lease 必须覆盖 producer、store、publisher 与 after-commit action 的整个边界；不能只在 `Document.save` 前瞬时校验 generation。否则 replace/delete 可在中途切换目录，让后半段写进旧代。
11. W06.2a 的 `projects` / `documents` / `entities` 不能通过普通 UoW 写入；只有 W06.2g importer 可写 terminal shadow，W06.2h sealer 可在 exact target 上追加 historical catalog 事实。authority 切换前生产运行时仍不得读取这些行。
12. W06.2b codec 的输出只是严格 JSON bytes，不是 business snapshot、`CommitReceipt` 或 authority 证明；不得让 codec import 业务模型、扫描 legacy 数据或写入 SQLite。
13. W06.2c snapshot set 只是已验证的内存投影，不是“已导入”或“已保存”证明；不得让 snapshot contract 扫描文件、写 SQLite、消费 OpLog/生产 adapter 或切换 authority；只有上层 W06.2g transaction+fresh reconcile 能产生 terminal-shadow 事实。
14. W06.2d RuntimeAdmission 只证明同步内存转换遵守 root/version/outcome/overlay/fence 顺序；在生产 SQLite adapter/cutover 完成前，不得把 test writer/fence 或 admission 称作数据库 durability。禁止公开 pending candidate、pre-admission ready、可复制 capability seal 或未验证 legacy detach 入口。
15. W06.2e scan handle/prepared receipt 只证明明确选择的 live 文件事实已在同一锁域 capture/verify 并在签发进程规范准备。不得绕过 topology/Document 双 context、持路径出锁后补读、把 explicit selection 冒充 catalog completeness、复用任一 one-shot lane、在 fork child 使用继承 carrier/reservation，或把 `consumed`/prepared 状态映射为 committed/durable/imported/authority。private bridge 必须 reserve/recover 后显式 consumed/release/quarantine，未知结果只能 quarantine。
16. W06.2f 的 schema migration 本身不是 importer。不得把 prepared 状态写入两张终态表、让 generic UoW 写五张 private 表、用非 namespaced/错 kind receipt 绑定 batch，或在业务表已脏、`legacy-import:` id 前缀/`legacy_import_v1` kind 已被占用时继续升级。
17. W06.2g terminal shadow 不是 authority cutover。不得绕过 fixed SQL/authorizer、在不同事务补 business/ledger/receipt、用 generic receipt 代替 exact reconcile、吞掉 async cancellation、让 bootstrap/runtime 消费 private bridge，或在缺 catalog completeness/source-current fence 时翻转 authority。
18. W06.2h historical catalog seal 不是持续 current 或 cutover proof。不得绕过 barrier strict enumeration、只做一轮 scan、把 tombstone/OpLog/assets/outbox/action 说成已覆盖、让 importer 与 sealer各自定义 sealed 正向结果、把 `cutover_ready=0` 改成可配置，或在缺 durable revision/writer fence/tombstone high-water 时切 authority。
19. Document atomic candidate 与 `AtomicCommitPlan` 都不是 commit receipt。不得在非 exclusive session 签发、重复签发、跨进程/线程复验、把 candidate 暴露到稳定 SDK、跳过 writer-side full closure revalidation，或按异常类型猜 session commit/rollback。
20. schema v6 是冻结的 private/unmounted substrate，v7 只能追加兼容修复。不得修改 v6 checksum/DDL，不得由普通 migration 填 runtime control/head/commit/receipt，不得让 generic UoW 抢占保留 identity；即使 private writer/reader/fresh reconcile gates 已完成，在 production issuer/cutover/rollback 与消费接线完成前，server/bootstrap/MCP/CommitCoordinator/PipelineSupervisor 仍不得消费它。
21. 不得用裸 `UPDATE repository_runtime_control.repository_revision += 1` 伪造“另一项目提交”。global revision 必须等于 durable commit history 的最大 revision；测试和生产都只能通过一笔闭合的另一项目 commit 推进它。不得把 writer/reader 功能正确性等同于 worker attempt 执行合同或 full-audit 性能达标。

## 7. 验证要求

本叶测试必须覆盖：

- producer/store/factory/publisher 均在共享锁内；
- lifecycle guard 覆盖 producer 到 action，guard 拒绝时 producer 调用数为 0；
- 并发提交不能让后版本先发布；
- pending/no-op 零副作用；
- Document → OpLog 的严格顺序；
- Document 成功、OpLog 失败的 `indeterminate`；
- batch 部分追加和全部事件接受；
- publisher pending/failed 不改变业务 committed；
- opaque store 异常按 unknown 处理；
- nested execute 在 store 前拒绝；
- CommandMutation 只调用一次 store，Operation 顺序不变；
- action 严格发生在 publish acceptance 后，失败不改写业务 committed；
- production PipelineSupervisor 缺 coordinator 时 fail-fast；
- producer/type 异常恢复同一对象图并原样重抛；
- 双 `NOT_DURABLE` 回滚后 receipt 归类 aborted，fake Document 保持兼容；
- rollback 失败输出稳定 `mutation_rollback_failed` 且保留 producer cause；
- UNKNOWN、部分 durable、committed 与 event failure 均不回滚；
- Python 3.9 可导入并运行。

SQLite kernel 还必须覆盖：

- 每个新连接真实读回 WAL / FULL / foreign keys ON / trusted schema OFF / busy timeout，而不是只检查初始化连接；
- application id、schema version、migration checksum 或已知 `sqlite_schema` 对象集合任一漂移均 fail closed；
- 两个进程同时首次打开只产生一份完整 migration，不留下半 DDL；
- WAL bootstrap 用独立私有锁有界串行，拿锁后仍须在迁移事务内重读版本；调用方不能取得底层 Cursor/Connection、结束 kernel 事务或改 kernel 私表；
- 路径组件、数据库、`-wal`、`-shm`、`-journal` 的 symlink/特殊文件拒绝，网络文件系统无法证明为本地时 fail closed，目录/文件权限私有；
- 只有显式 initialize 能创建目录/数据库；commit/inspect/reconcile 在 authority 缺失时零创建并 fail/indeterminate；W06.1 的可用支持面明确为 POSIX 本地文件系统；
- 两个独立进程不能为同一 transaction id 产生多份 receipt；
- COMMIT 前 `os._exit` 后 reconcile 不得 committed；COMMIT 后、ack 前 `os._exit` 后必须找回同一个 committed receipt；
- crash 测试用进程同步点，不用 sleep 猜时序，不从父进程继承连接；
- v1→v2 迁移保留旧 repository marker/receipt，建立精确 STRICT 业务 schema；之后各版只追加事实并冻结旧 DDL/checksum；v7→v8 只允许 pristine legacy transaction namespace。旧代码读超前 schema fail closed；
- `GATE-DB-01..03` 在任何 surface profile 上执行相同 runtime 证据。
- deterministic codec 对完整严格 JSON 值类型、正负 4300 位 int 接受/4301 位拒绝、dict 顺序无关的固定向量、纯函数性、严格逆变换、32-byte SHA-256 和非法/非 canonical 输入拒绝给出真实运行证据；
- `GATE-DB-04` 在任何 surface profile 上执行相同 codec runtime 证据，同时检查 codec 未引入 Document/Entity/SQLite 依赖。
- business snapshot 必须覆盖 project/document/entity 精确投影、软删包含、read-only 显式排除、closed envelope、payload—列值绑定、规范 Entity 集合根、恢复后逐行重投影，以及容量/非法值 fail closed；
- `GATE-DB-05` 在任何 surface profile 上真实 capture/verify/restore，证明删减、多添和重算单叶 hash 都与未同步改写的 project root 不一致，restore 不经 `Document.open` / `Entity.from_dict` / `Document.add`；同时静态检查 snapshot 模块无 SQLite/file/topology/sdk/OpLog 依赖，并在 fresh isolated current schema 上重证 authority 仍为 legacy、全部 private 表初始为空。它不把无密钥 root 误报成对全树一致重写的真实性证明。
- `GATE-DB-06` 在任何 surface profile 上真实执行 rooted restore handoff、changed declaration、exact-one version、root/version CAS、commit 后 overlay、changed-row schema 拒绝、rollback 与 one-shot admission；同时扫描 state/projection/coordinator 三层，静态证明依赖单向、所有公开对象原样 re-export、runtime seam 不依赖 storage/topology/log/cache/I/O 实现且未公开 candidate/proof/unverified legacy issuer。
- strict legacy scan/prepare 必须覆盖 explicit project/generation、外层 topology generation/root identity 与内层 Document content lock 共存、outer/inner exact head、capture 后 verify、read-only exclusion 拒绝、规范项目/ordinal 排序、跨项目 Document 唯一、非空 source head、虚拟 framing 固定摘要、manifest/package/receipt 绑定、完整 immutable plan 逐字段映射、整批失败零消费、payload/runtime lane 各自 exact-once、私有 adapter exact-one reservation 与显式三态决议，以及伪 carrier/错配 receipt/篡改/并发/fork inheritance fail closed；进程重启不得把旧内存 receipt/plan 当作耐久事实。
- `GATE-DB-07` 在任何 surface profile 上真实执行 v2/legacy live project scan、prepared package pairing、完整 plan 与 canonical manifest/snapshot/source descriptor 对账、row graph 隔离、opaque issuance、exact pair binding/recovery、两条独立 one-shot lane、fork-inherited reservation 拒绝、release/consumed/quarantine 三态与 runtime admission；静态证明 plan 不进入稳定公开面、scanner 只能消费 topology+Document 私有 scan seam、state ← facade 单向依赖、没有原始路径/SQLite/kernel/OpLog/cache/I/O 旁路，并把 private plan 的唯一下游钉为 W06.2g SQLite bridge。
- `GATE-DB-08` 以冻结的 v3 registry 真实执行 fresh v3 与 v2→v3，证明 import substrate 历史合同；current fresh/upgrade 继续到 v13，且不能改变旧 migration checksum/DDL。脏 v2 或预占 import namespace 仍须在任何 v3 DDL 前整笔回滚。
- `GATE-DB-09` 在任何 surface profile 上真实执行 EMPTY→TERMINAL_IMPORT 首导、exact retry、same-id/cross-id conflict、disjoint batch、row tamper、fixed SQL/authorizer rollback、database/finish acknowledgement loss与 async classification；静态证明共享 import integrity、七条 plain INSERT、唯一 bridge、无公开/bootstrap/runtime caller，并重证 authority 始终为 legacy。
- `GATE-PROJECT-09` 真实/静态证明 barrier SH/EX 全生命周期、barrier→repository→project→Document 锁序、严格 full-catalog fail-closed 枚举、fork child close-without-LOCK_UN 与零公开 surface。
- `GATE-DB-10` 真实执行 fresh/v3→v4、empty/nonempty historical seal、全目录双扫描、exact retry、namespace collision、固定四 INSERT/authorizer、ack-loss/async、seal/member 等长篡改以及 importer/sealer 同一分类；静态限定唯一 private coordinator，并重证 legacy authority、固定 `cutover_ready=0` 与 live-only 范围。
- `GATE-PROJECT-10` 真实崩溃三个 admission 安装耐久边并 fresh recover，验证唯一 production rollout caller、普通入口 pre-I/O 拒绝和 live+deleted 完整快照。
- `GATE-DB-11` 冻结 v1–v5 checksum/DDL，验证纯 repository digest、deleted high-water、三张 STRICT/private/append-only 表与耐久 `legacy/cutover_prepared/0` header，且不把 proof substrate 误报为 cutover。
- `GATE-DB-12` 执行四种仓库形态的 exact seal/retry、固定四 INSERT、partial/receipt/tamper、ack-loss/async/durability 与多进程收敛，并静态证明唯一 private writer/bridge、production caller=0。
- `GATE-PROJECT-11` 证明同一 EX→EX barrier 覆盖 S0→v4/current projection→S1→v5 fresh reconcile→S2，PRESENT 不触发旧 v4 writer、active plan 必须释放且 admission CAS 不可达。
- `GATE-DOC-10` 真实验证 opaque one-shot Document candidate 的 exclusive-session/process/thread/head binding，以及未提交时原位恢复对象 identity/version/timestamp。
- `GATE-COMMIT-08` 真实构造全成员 AtomicCommitPlan，重证 closure/candidate head，并确认纯 integrity dependency boundary 与底层篡改拒绝。
- `GATE-DB-13` 冻结 V1–V6，明确 V6 checksum=`41fe…`，验证其 DDL-only substrate 在 current V13 fresh 仍为17张STRICT/private空表、legacy marker未变且generic UoW不能抢占保留identity。
- `GATE-PROJECT-12` 是冻结的历史 V12 证据：静态与运行时证明 V6 plan/candidate production caller/export 为零，且当时普通 V12 topology/kernel 启动不挂载、不翻 authority、17 张 V6 transaction 与 32 张 production/runtime 表全空；current V13 的17+36空启动边界由后续门继续证明。
- `GATE-COMMIT-09` 真实执行 same-transaction writer 的 pre/post-COMMIT、process-control、ack-loss 与 durability-barrier fault matrix，并冻结 change manifest v2 / baseline 独立 hash domain。
- `GATE-COMMIT-10` 在 Worker 投影、首次 OUTBOX CLAIM 与 schema-v9 `AUTHORIZE_EFFECT / COMPLETE / RECONCILE` fixed-SQL 垂直之上，继续冻结通用 pipeline/action/multi-outbox production writer、transition CAS、reserved control identity preflight、同 snapshot plan-free rooted reader、多 allocation/pipeline-control durable audit和合法 Worker 后缀重联；聚焦回归覆盖事务故障、ack-loss、篡改、I/O、资源预算、五态分类与第二轮 runtime/worker/dispatch publish fence。不支持的 renew/expire/retry、ACTION/PIPELINE worker execution和 atomic-v2 扇出必须 UNKNOWN。
- `GATE-COMMIT-12` 静态锁定private install writer只接完整重验的V12 closure projection、16条exact plain INSERT的列和值位次、唯一marker CAS、完整authorizer、helper外无mutation、V3 terminal-shadow三表只读、legacy/production receipt CAST-aware闭包、唯一coverage/stage结构、closure-last、同snapshot恰好两次fresh语义读回、COMMIT后fresh reconcile、V12 identity与server/bootstrap/sdk/production caller为零。独立runtime probe覆盖empty install/rollback/retry/ack-loss/durability、真实BUSY、同/异projection双线程竞争、production/legacy BLOB污染、rollback/commit-close uncertainty、stable kernel CORRUPT与process-control原信号；live/mixed/deleted、asset coverage metadata、partial namespace、额外V3行和active suffix由focused writer suite另证。本门不声称已有真实多进程竞争或等长语义篡改证据，也不冒充cutover auditor/coordinator/router。
- `GATE-COMMIT-13` 静态锁定global cutover auditor只接database+optional expected、query-only双snapshot、六恢复点、五态、逻辑/VM预算、V12 identity、detached非权限与production caller为零。首张INSTALL证书只按已知因果后继重建exact receipt root；后续periodic证书验证raw/parent/predecessor/worker-tail membership/final-head与current上界，不用毫秒时间戳伪造历史as-of，在没有authenticated checkpoint时保持UNKNOWN。独立probe用真实writer/文件覆盖pristine/coverage-only/stage-only/mixed preinstall、headless install、五个head阶段与逐字段identity、journal漂移不修复、expected冲突、carrier/BLOB/scheduler/额外row/future-time/bad-audit、首证书root、合法/任意root/未来高水位periodic、真实worker suffix与head回退、closure I/O、TEXT/BLOB/VM预算、逐字段fresh fence、durability和snapshot/progress/cleanup三类process-control；focused suite另证live/deleted/mixed/asset install及periodic suffix不能遮住mount篡改。它不冒充phase writer、coordinator/router、真实多进程crash、硬RSS/page/time门或authenticated checkpoint。
- `GATE-DB-14` 冻结 v1–v6+v7，真实执行 authentic v6→v7 与合法 A1→B1→A2，要求 global/project/head=`3/2/2` 且 fresh reconcile committed。
- `GATE-DB-15` 真实读取 snapshot/Operation/outbox/action/pipeline/asset/inbox/confirmation 全闭包，并验证 pinned read 后重叠提交按第一个 `BEGIN` 线性化。
- `GATE-PROJECT-13` 对 baseline/cutover/asset/audit/activation/mount/phase-binding/atomic-control/worker 纯合同、历史 V7/V8/V9/V10/V11 runtime/writer/reader、current-V13 production authority/writer/rooted reader、install-closure semantic reader、phase-binding mapper/reader、private V12 install writer、neutral cutover verifier、global cutover auditor、phase-1 finalizer、asset-provider writer、first-claim/payload writer、worker durable auditor与专用test-support做全树反向引用/import/export审计，并逐项冻结capability helper的精确forward dependency、reverse consumer、call site和reference site。asset-provider writer已经存在但仍private/unmounted，external observation issuer与production caller为零；本门不证明audit/activation/mount三个writer、coordinator/router或dispatcher完成。普通启动仍legacy、无runtime control，17张V6 transaction与36张production/runtime表全空。
- `GATE-DB-16` 永久冻结历史 schema-v8 三项 identity，显式在 V8 registry 内真实执行 fresh/pristine-V7 upgrade、17+25 空表、脏 V7 preflight 整笔回滚与旧 V7 writer fence。
- `GATE-DB-17` 永久冻结历史 schema-v9 三项 identity、exact 25+3 表所有权、authentic V8→V9、脏 V8 原子回滚与旧 V8 writer/reader fence；它只证明 sidecar 结构与迁移边界，writer/auditor 的事务和审计语义由 `GATE-COMMIT-10` 另行证明。
- `GATE-DB-18` 冻结历史 schema-v10 三项 identity，真实验证零新表、V8/V9 前缀不变、34 个 timestamp guard、2 个 dispatch tuple guard、24-byte `CHECK(NULL)` 垃圾与部分 tuple 拒绝、authentic V9→V10、脏 V9 原子回滚及旧 V9 writer/reader callback-zero fence；它不冒充 production mount、V11 upgrade、dispatcher、cutover 或 router。
- schema-v11 focused evidence 真实执行 fresh/authentic-empty-V10→V11、脏production/nonlegacy V10原子拒绝、旧V10 reader fail-closed、mount反序与split revision拒绝；head focused evidence覆盖五态fixed vector、逐pointer后继影响、前驱/顺序/预算/非法类型篡改；topology evidence覆盖freeze exact retry、ack-loss、进程崩溃恢复、unsafe carrier与全部普通legacy入口pre-I/O fence。
- schema-v12/install-closure focused evidence 必须真实执行 fresh/authentic-empty-V11→V12，证明 V1–V11 identity 逐字不变、只新增 4 表且 `PRODUCTION_RUNTIME_TABLES=32`；覆盖 16 MiB/4 块/64 MiB 边界、稠密 chunk ordinal/offset、缺块/多块/等长篡改、连续 live repository revision、baseline inner mirror、三组空 target 根、`available` revision 1 + fresh coverage、closure 后追加拒绝与 initial-head 前驱。fresh semantic reader 还必须证明 sidecar member ordinal 精确映射（不把 deleted 导致的空洞误判为损坏），并拒绝仅关系上自洽但 artifact/outer/closure 语义 hash 错误的库；本门不得借 fixture 写入冒充 fixed writer/coordinator/auditor。
