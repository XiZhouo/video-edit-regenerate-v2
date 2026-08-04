# 07 Persistence Commit

这一叶只解决一个问题：**一次编辑什么时候可以诚实地说“已经保存”**。

过去 HTTP、MCP、CLI、管线、撤销和重做各自执行“改内存、保存文档、追加日志、广播”，顺序并不一致。最危险的情况是界面已经收到广播，随后保存失败；用户看到新内容，重启后却消失。这里把所有入口必须共用的提交缝收成一个：

```text
项目 activity lease + 共享锁内：mutation session → producer → CommitStore.commit → session commit/rollback
                                               → canonical events → publisher 接受 → after-commit actions
                         │
                         └─ FileCommitStore: Document.save → OpLog.append
```

历史 W06.1–core 3.29.1 另加了一块尚未接管生产业务写入的 SQLite 事务 kernel、schema、纯 codec、业务快照、legacy 迁移证明与原子业务读写。V1–V11 的 DDL、checksum 与历史语义全部冻结；当时的 current V12 只追加 install-closure 所需的 4 张私有表，用分块 artifact 补齐完整 member mapping carrier，并用 baseline-specific atomic sidecar 合法表示发生在 mount 之前的 baseline outer receipt。每块最多 16 MiB，单个 artifact 最多 4 块、64 MiB；空 targets 沿用 pipeline/action/outbox 三组全空的既有规范，没有另造第二套空值算法。baseline 中的 asset blob 仍是 `available` revision 1，必须由 fresh exact coverage 背书，不会伪造 `staged → available` revision 2 历史。3.27.0 在完全不改V12 identity的前提下增加private fixed-SQL install writer：它只消费完整重验的closure projection，在一个`BEGIN IMMEDIATE`中按冻结列写入，closure最后写，同事务双读回并在COMMIT后fresh reconcile。首条写入前还会核对V3 shadow、V5/V8前驱、production-reserved receipt精确补集和SQLite数据库时间；孤儿worker/pipeline/audit/activation/mount回执、未来install时间或不可靠cleanup都不能被误报为成功。该事务会把**数据库内部**的`kernel_repository.authority`从`legacy` CAS为`sqlite`并建立read-only runtime；这只是SQLite安装态marker，不是文件admission CAS，也没有改变生产authority或路由。writer会写install-owned的asset blob/ref物化行：身份来自baseline，MIME、extension与byte length由同一份fresh coverage descriptor补齐；它不写provider、stage或coverage事实。non-empty V3 terminal shadow已有的`projects/documents/entities`只允许exact只读核对，绝不属于writer-owned DML。3.28.0又增加private plan-free cutover durable auditor：它从一个pinned snapshot自行重建headless install closure和五个canonical head阶段，再用fresh snapshot fence发布；detached结果只用于诊断，不是下一阶段写权限。3.29.0在仍不改V12 identity的前提下补上`SQLiteCutoverPhaseHeadWriter`：这个private phase-1 finalizer只接exact `SqliteDatabase`与模块私有capability，不接detached auditor结果当写权。它在一个`BEGIN IMMEDIATE`内从raw rows重证install generic/specialised receipt、V12 closure、`read_only` revision-1 runtime和空post-install suffix，然后只用唯一fixed `INSERT`建立`INSTALLED_HEAD`；COMMIT后必须fresh reconcile并通过durability barrier。3.29.1再把auditor与finalizer共用的行/字节/VM预算、reserved receipt闭合和不可用分类抽到只读中性leaf，消除writer借用auditor私有实现的反向耦合；错误码、五态和V12 identity不变。四类direct-predecessor binding在3.29.1当时仍属后续；3.30.0/V13补齐carrier、正反guard与fresh语义重建，3.31.0又增加第一段可执行的private asset-provider writer，但audit/activation/mount三个writer、coordinator和production router/caller仍未挂载。所有SQLite适配器与当前文件写路径仍是并列关系，不是dual-write：

```text
生产业务 authority ──> FileCommitStore（仍可能 indeterminate）
事务能力地基       ──> SQLite kernel（独立证明 transaction/reconcile）
                                  └─ v6 已定义 Document/Operation/outbox/action 等原子合同
                                  └─ v7 prototype writer/reader 只保留为历史兼容证明
                                  └─ v8 新增 25 张 authority/worker/outbox 耐久表
                                  └─ v9 新增 3 张 effect/result/proof payload sidecar
                                  └─ v10 新增 34 个时间 guard + 2 个 dispatch tuple guard，零新表
                                  └─ v11 仅空 legacy production namespace 重建 mount/head 约束
                                  └─ v12 新增 4 张 install artifact/baseline atomic/closure 表
                                  └─ install writer 固定SQL写入V12 closure并双读回/fresh reconcile
                                  └─ global cutover auditor plan-free重建六个恢复点并fresh fence
                                  └─ phase-1 finalizer 独立重证headless closure并只写initial head
                                  └─ neutral verifier 单一实现预算/receipt namespace/不可用分类
                                  └─ production writer 同事务写 pipeline/action/multi-outbox
                                  └─ Worker writer 仅支持 OUTBOX 的授权 effect/完成/对账
                                  └─ rooted reader 从同一 snapshot 重建 production commit
                                  └─ plan-free auditor 重建多 allocation、control 与 Worker 闭包
                                  └─ production router/cutover/consumer 仍未挂载
迁移影子链         ──> catalog barrier EX → repository EX → 两轮 strict scan
                                  └─ terminal shadow → historical catalog seal
cutover source      ──> exact terminal live/deleted snapshot → durable source freeze
                                  └─ asset stage/coverage ≤ install ≤ audit ≤ activation ≤ mount
耐久账本           ──> SQLite schema v13（历史 v1–v12 冻结 + direct-predecessor bindings）
                                  └─ v12 install closure carrier；v13 四类 phase-binding carrier/guard
                                  └─ sqlite_cutover_phase_binding_reader 逐字段重建 binding 与完整 head 链
                                  └─ fresh/upgrade 只建空表，不自动翻 authority
                                  └─ current v13 普通安装后 17 张 transaction + 36 张 production/runtime 表全空
```

core 3.30.0 先把四类direct-predecessor carrier、正反guard与fresh语义重建交付为current V13；3.31.0在不改V13 identity的前提下继续补上第一段可执行的private数据库转换：`SQLiteCutoverAssetProviderPhaseWriter`。四阶段binding的表名、固定列序和值投影现在由一个纯mapper唯一拥有，reader会把重建后的binding重新投影并对存储行逐列核对，fixture与writer不再各自拼列。asset writer不接detached plan或预签binding；它在自己的唯一`BEGIN IMMEDIATE`内从raw rows重证install closure、coverage、read-only runtime、receipt与scheduler checkpoint，将事务外READY身份逐字段绑定到事务内快照，然后自己签发provider evidence/binding，只做91列fixed binding INSERT→canonical head CAS UPDATE→同事务semantic readback。外部provider/admission/maintenance事实由同PID、同线程、active私有carrier带入，exact retry还必须逐项匹配pending admission、source freeze、maintenance scope/proof/observed time和provider checkpoint。该carrier目前只有测试issuer且production caller为零；未来coordinator必须让严格catalog EX→repository EX租约和provider snapshot覆盖完整调用。当前`user_version=13`、36张production/runtime表及schema三项identity不变；audit/activation/mount writer、coordinator/router仍不存在，生产仍走`FileCommitStore`。

## 已实现

- `CommitCoordinator.execute(producer, trace_id="") -> CommitExecution`
  - 仅调用 producer 的窗口进入 ContextVar `producer` phase；store/event 回到 `idle`，已提交 action 进入 `after_commit`。框架 effect port 可据此硬拒提交前文件/网络/线程副作用，且 async task 之间不串 phase；
  - Document 支持 `begin_mutation_session()` 时，producer 前先保存内存快照；
  - producer 抛错或返回非法类型时先恢复原对象图，再原样抛出原异常；
  - store 明确证明 Document 与 OpLog 都未写入时恢复快照，并把 receipt 从 `dirty` 归类为可安全重试的 `aborted`；
  - 任一耐久结果为 `unknown` / `durable`，或已经进入事件阶段时绝不回滚，避免内存落后于磁盘；
  - 回滚本身失败统一抛可被 HTTP/MCP/CLI 识别的 `mutation_rollback_failed`；
  - producer 只返回 `None`、`PendingConfirmation`、`Operation`、`MutationBatch` 或 `CommandMutation`；
  - `None` 与待确认请求零持久化、零事件，并恢复 producer 意外留下的内存改动；
  - 所有真实 mutation 只调用一次 `CommitStore.commit(intent)`；
  - 只有 receipt 同时证明 Document 与 OpLog 耐久，才会构建和发布事件；
  - 3.40 延续 3.39 的 v2 sealed complete-history preflight，并把 exact coordinator `Document` 一并绑定到 Operation seal、permit 与 attestation；同 id 的外来 Document 也不能消费证明。实际 OpLog append 仍逐条消费 exact permit/oplog/order authorization，Start 必须是整笔首项且声明 ids 与实际 tuple 完全相等；raw/unbound writer 无权落 v2；
  - receipt 只要不能证明 Document 与 OpLog **双 NOT_DURABLE**，就先撤销该 run 旧 attestation；committed+双 durable 后再从 exact FileCommitStore/OpLog fresh 重证并原子安装新 attestation。dirty/indeterminate、write-scope/history/session finalizer 中断、head漂移和 fresh failure 都保持零新证明且 typed replay-unsafe；
  - Pipeline durable reconcile 先于 event build/publish；所以事件失败不会抹掉磁盘已经证明的闭包，也不会授权业务命令重放。attestation 只在 private registry，不进 receipt/event/SDK，也不是 worker grant；
  - 3.40 的 dormant delegated request 只在 coordinator 外做 deny-only 重验：先核对 exact FileCommitStore/Document/history graph，再无反向持锁地 fresh 读取 OpLog、重归约当前 history，最后检查原业务 policy 与请求 scope。它不调用 store/coordinator、不执行 mutation；发出后任何 head 变化都会让请求失效，Batch 5 必须把同一检查收进真实写事务并叠加 live grant；
  - `CommitExecution.events` 是锁内从准确提交后快照生成的唯一 payload，下游入口直接消费，不能再次推导；
  - 事件失败不推翻业务提交，receipt 仍是 `committed`，独立标记 `failed` 或 `pending`。
  - producer 内递归 `execute()` 会在任何 store write 前以 `nested_commit_forbidden` 失败；多条 mutation 必须显式组成一笔提交。
  - 可注入 `lifecycle_guard`；生产环境由 ProjectTopology 提供。它在 producer 前核对 generation，并一直持有到 store、event 和 after-commit action 完成。过期 Context 在产生任何新 mutation 前即被拒绝。
- `CommandMutation(operations, after_commit)`
  - 把主命令、占位等派生 Operation 放进一个 commit id / receipt；
  - worker 启动、pipeline 控制、manifest 和状态广播只能登记为 `DeferredAction`；
  - action 仅在业务双耐久、canonical event 已被接受后运行；失败单列 `ActionReceipt`，不得重放业务命令。
- `FileCommitStore(document_path, op_log)`
  - 固定执行 Document 后 OpLog；
  - Document 已成功而 OpLog 未成功时返回 `indeterminate`，禁止自动重放整条命令；
  - 不依赖、不调用任何 broker，因而不可能提前广播；
  - 支持单个 Operation 和同一逻辑提交下的 MutationBatch。
- `BrokerEventPublisher` 把现有 `RealtimeBroker.publish_change` 接到新事件端口。
- SQLite transaction kernel + schema registry + deterministic codec + business snapshot/runtime-load/legacy-prepare/terminal-shadow/catalog-seal contract（W06.1–W06.2h）
  - 当前支持面是 POSIX 同机本地文件系统；只有显式 initialize 可创建数据库，其余打开路径一律 no-create；
  - 每个连接固定验证本地文件系统、私有权限、WAL、FULL、foreign keys、trusted schema、应用标识与迁移 checksum；
  - `transaction_id + kind + committed_at` 组成最小 kernel receipt，新连接可 fresh reconcile；
  - 独立进程与真实崩溃测试证明 COMMIT 后丢响应可找回，COMMIT 前退出不会冒充成功；
  - 唯一可调项是写锁等待 `ZE_SQLITE_BUSY_TIMEOUT_MS=5000`，安全基线不能降级；
  - v2 migration 新增空的 `projects` / `documents` / `entities` STRICT 表，并预留 codec、snapshot format、payload BLOB 与 SHA-256 元数据；三表仍是 kernel private，普通 unit of work 无权写；
  - v2 因此要求 SQLite `>=3.37.0`；旧引擎 fail closed，不会退化创建非 STRICT 表；
  - v1 已发布 checksum 算法被冻结保留，v2 起使用精确 UTF-8 DDL checksum；升级后会验证迁移账本与真实 `sqlite_schema`；
  - v3 migration 新增 `transaction_receipts(transaction_id, kind)` 的精确绑定索引，以及 `legacy_import_batches` / `legacy_import_projects` 两张 `STRICT`、kernel-private 历史账本；batch 将 import id 与 namespaced transaction、receipt protocol/terminal status、manifest/payload/batch protocol、显式选择、counts/bytes 与三组摘要绑定，project 行再按 ordinal 绑定 generation、Document、source head 与 business root；
  - 两张账本只保存“同一事务已提交”的终态记录，不保存 `prepared_verified_payload`、carrier reservation 或进程内 one-shot 状态；新初始化/纯升级仍为空，只有唯一 private terminal-shadow bridge 能写入；
  - v2→v3 在执行任何 DDL 前拒绝已存在的业务表行，或 transaction id 占用 `legacy-import:` 前缀、kind 占用 `legacy_import_v1` 的 receipt；失败整笔回滚，不能留下索引/半表或推进 `user_version`；
  - schema v3 运行期间，通用 kernel `commit` 同样拒绝该 transaction-id 前缀或 kind；唯一受控 importer 在“业务行 + terminal ledger + exact receipt”同一事务里使用它，普通 transaction 不能抢占身份；
  - v4 migration 在冻结的 v3 substrate 之后新增 `legacy_catalog_seals` / `legacy_catalog_members` 两张 append-only `STRICT` 历史表。seal 固定绑定 canonical live-project descriptors、exact import、schema v4 fingerprint、source/target/seal 摘要和 `cutover_ready=0`；空目录用 `import_id=NULL + 0 members` 明确表达，不伪造空 import；
  - catalog transaction 使用旧 generic receipt 语法无法表达的私有 prefix/kind，健康 v3 中曾合法存在的相似普通 receipt 可以原样升级，不会永久抢占 v4。v4 打开后 kernel 只接受四种闭合状态：全空、仅 terminal import、空目录历史封印、非空目录历史封印；marker、seal、member、import batch 或 receipt 缺一/多一/错配都会 fail closed；
  - v5 只追加 writer-admission 与三张 repository proof 表。普通文件入口在任何 repository/project/Document I/O 前必须看到 `legacy` admission；安装用 prepared/record/committed 两阶段记录可崩溃恢复，只有 production Repository bootstrap 能升级严格闭合的 pre-v5 store；
  - private strict repository snapshot 同时封装 live Document head 与 deleted tombstone/generation high-water。v5 exact writer/reconcile 已能把它与 repository epoch/revision、v4 catalog、runtime/code identity 逐字节绑定，并在同一 topology barrier 内完成 S0/S1/S2；header 只能是 `authority=legacy / authority_state=cutover_prepared / cutover_ready=0`；当前仍无 production identity issuer、挂载入口和 cutover coordinator；
  - v6 在冻结 v1–v5 checksum/DDL 后只追加原子业务事务 substrate：runtime control、project/head、commit/operation/target、inbox/outbox、confirmation、durable action/attempt、pipeline job/attempt/transition、asset metadata/commit-scoped exact occurrence ref 共 17 张 `STRICT` 私有表；初始化与历史升级后这些表必须全部为空，kernel marker 仍为 `authority=legacy`。asset ref 是 append-only 版本历史，当前 head 只选择当前 commit；V5b.1 默认不做 GC，后续必须先定义 retention/compaction 才能释放历史资产；
  - v6 已发布 checksum 永久固定为 `41fe4ba13a35bacfa75af648381c0dadc4d256a8872c65a1bbf0138b64252254`。schema-v7 `business_local_revision_separation_v1` checksum 固定为 `d96606b2d6b47ee42e53dd216e7c711e00c877573814c4cc73526004e8aa0656`，只追加 local revision 兼容修复：global repository revision、per-project revision、per-Document-head revision 各自独立；空 namespace 直接升级，旧 prototype 数据只有通过单项目连续历史 preflight 才能回填/重建 FK，否则整笔失败；
  - `AtomicCommitPlan` 是纯内存、immutable、可完整重验的 private-writer 输入：它闭合 after snapshot、Operation hash chain、逐 operation outbox、可序列化 action/pipeline intent、schema-aware asset occurrence、可选 inbox/confirmation 与 candidate head；change manifest 因三 revision shape 显式升为 protocol/schema/hash-domain v2，bootstrap baseline v1 改用独立 `business_baseline_contract` hash domain；
  - private runtime 已把两种成本明确分开：transaction fast fence 只做 current schema/security、SQLite marker、runtime control 与 indexed `global=max(commits)`，reader/writer/reconcile 热路径不再扫描历史；显式 `startup_business_runtime_full_audit` / `periodic_business_runtime_full_audit` 才执行 `PRAGMA integrity_check(1)`、全 FK 与 reserved receipt↔commit 双向闭包。`inspect_business_runtime` 保持 startup full 语义，只有 active profile 能生成 writer binding；两层都没有 activate/mount/cutover API；
  - private SQLite writer 用一个 `BEGIN IMMEDIATE` 同笔写 current v2 snapshot、receipt/commit、Operation/target/outbox/action/pipeline/asset、inbox/confirmation 与三个 head，再逐组 closure 并强制新连接 fresh reconcile。pre-COMMIT failure 全回滚，post-COMMIT ack-loss 只能精确重联，durability barrier failure 只能 UNKNOWN；
  - private rooted reader 以第一个 `BEGIN` 为线性化点，在同一 snapshot 重证 head/commit/receipt/snapshot/所有 member group/manifest，返回 detached carrier；第二个 fresh snapshot 只拒绝 authority/control 不兼容变化与 global revision 倒退，不虚构“返回时一定最新”；
  - schema-v8 `production_authority_worker_runtime_v1` 在冻结 v1–v7 后只追加 25 张 `STRICT` 私有表，承载 install/asset coverage/audit/activation/mount、outer atomic receipt、worker target/current/receipt/result/proof/ledger 与 outbox allocation/dispatch head。它的历史 migration checksum=`f6c8a8e1f2aa633a52da52a04e6b58ad11230f5093e4b883e7b63674bb8ad80b`、prefix fingerprint=`0d96ffa4e88ac3c3d3fd1ba1b6f09f11a3a2d7a0e1861f5781bf0856d7fce771`、prefix migration root=`9595ea544b5b055f71d2c93619e49b3353a5d48424951fc410b0ca2484c1834a` 永久冻结；pristine legacy v7 才能升级，历史 17 表有数据、非 legacy authority 或 reserved namespace 已占用会在任何 v8 DDL 前整笔拒绝；
  - 历史 schema-v9 `worker_payload_projection_sidecars_v1` 只在冻结 V8 后追加三张 `STRICT` sidecar：effect request、result/error 与 proof payload各自唯一拥有 exact schema/codec/version/bytes/hash五元组，同时保存 projection metadata与closure。V9 checksum=`23018e6b59b8d3ad235e3110abd122ba12584cee272cff0614730f4d4be0dba0`、prefix fingerprint=`ccb5b4d248c2c429c0a99a5811816521cf4563de3bee457db826b4e4d4090ddf`、prefix cumulative root=`32ade765a739bca3427b4901fa4eee6a737d9d4a0519c369edd2deea56d7b26a` 永久冻结。V8→V9 只接纳 pristine 或完全闭合的 claim/pipeline-control V8 history，其他 worker operation、effect/result/proof、孤儿 receipt、断链或未 headed suffix整笔回滚；
  - 历史 schema-v10 `runtime_timestamp_dispatch_hardening_v1` 不新增或重建任何业务表，也不改 V8/V9 identity。它为 V8/V9 共 32 个弱时间列及生产保留 `transaction_receipts` 父行安装 34 个 timestamp trigger：required 值必须是 exact 24-byte、可解析、可原样格式化的毫秒 UTC，nullable 值只能为 NULL 或同样 canonical；append-only 表守 INSERT，可变 head/current 同时守 INSERT/UPDATE。另有 2 个 tuple trigger 要求 `outbox_dispatch_head` 的 published 只能是 `sequence=0 + 空 pointer` 或完整正数三元组，inflight 只能全 NULL 或是 `published_sequence + 1` 的完整三元组；
  - V9→V10 preflight 会在任何 V10 trigger 或版本推进前扫描这些时间与 dispatch 形状。坏 V9 直接整笔拒绝且不自动修复，健康 authentic V9 才能原子升级；旧 V9 reader/writer 看到超前版本时必须在 callback/业务 SQL 前 fail closed；
  - historical schema-v11 `mount_chronology_state_closure_v1` 永久保留 V1–V10 registry前缀，只在 authentic V10、`authority=legacy`、runtime/business/production/reserved-receipt namespace全空时重建 mount/head 两张空表。它把 mount receipt时间固定为 `coverage ≤ install ≤ mount`，并把 head固定为 `state_revision = phase_revision`、typed pointer按阶段累加、直接下一阶段 CAS。任一 mounted、active或部分 production V10都在第一条 DDL 前整笔拒绝；V11不承诺把历史 mounted DB原地升级；
  - historical schema-v12 `cutover_install_closure_v1` 不改写 V11，只在 authentic且 production namespace 仍全空、既存 V5 source receipt 规范闭合的 V11 上追加 4 张 `STRICT` 表：install artifact 头、artifact chunk、baseline atomic sidecar 和 install closure。V12 checksum=`aa61960d2bd4e10a41d1ef51767bf4f4809f5fab26cae5ebef3bfa30fccf35a2`、schema fingerprint=`4c265275dcf7d90e4ee77fda3b3a1749646e5e55783ce17fdff40e737195b226`、cumulative migration root=`1537102bc6fe371ceef49a58b566dbd6f86940eaf838dcfeb263e3df37cbb77e`。历史 28 张 production/sidecar 表加这 4 张等于 32；V12当时不建四类binding，这一历史边界已由current V13 migration向前推进；
  - V12 的两类 artifact 是 `member_mapping` 与 `baseline_projection`；chunk ordinal/offset 必须稠密，非末块必须满 16 MiB，总大小不得超过 64 MiB。每个 live baseline commit 另有一条与原 `atomic_business_write_receipts` 内层列精确镜像的 pre-mount sidecar；其 outer manifest 不需要尚未发生的 mount，且 `target_count=0` 必须对应既有三组空 target 根；
  - install closure 会从关系上要求两份 artifact 块数/字节数完整、live baseline sidecar 数量与 repository revision 连续，并以十个 exact 指针绑定 V5 source 与 V8 coverage；initial cutover head 必须先看到 closure。source generic receipt 首次写入即要求 canonical UTC，随后 kind/prefix/time 永久不可改；premount 的 36 张物化表只有在 mounted head 与 exact active runtime 同时成立后才解冻；
  - shared stored-manifest parser 统一 authority/provenance 对 install 与 coverage bytes 的解释。provenance reader 会重建 V5 proof、每个 V8 stage/member 与全仓 asset coverage，只签发 issuer-stamped binding；closure reader 再从原始块、inner receipt mirror 与 baseline outer 列重建纯合同并重算 V12 摘要。空 install 保留 runtime revision 1，首个 post-mount commit 必须是 revision 2；full audit 从 closure 推导这一处偏移，同时仍可在 phase 1/2 read-only 阶段运行；
  - 这组 fresh reader 是 startup/full-audit 级全证明，可能遍历全部 source/stage/baseline 并解码大 carrier，不得直接塞进每笔编辑热路径。3.27.0 的private install writer只在install事务和fresh reconcile消费它；3.28.0 global auditor同样属于startup/recovery冷路径，并使用`mode=ro + query_only`连接发现而不修复persistent profile漂移。它能用明确的因果后继集合精确重建首张INSTALL证书的receipt root，拒绝高于current append-only head的周期证书，但不会用业务时间戳猜periodic证书的历史as-of；stage-only/stage+coverage preinstall、periodic certificate suffix和普通post-mount suffix在缺少authenticated checkpoint时都保持UNKNOWN。当前逻辑门限制行数、TEXT/BLOB字节、证书与SQL VM步数，reserved receipt流式比较，但不冒充硬RSS/page-count/墙钟保证。后续production router仍需authenticated checkpoint/小型hot fence与真实百万行性能门，不能把writer或auditor green写成production cutover已完成；
  - `SQLiteCutoverPhaseHeadWriter` 是core 3.29.0的private phase-1 finalizer。它只将已耐久但headless的install closure收口为`INSTALLED_HEAD`：构造时只接exact `SqliteDatabase`与模块私有capability，不接plan/projection、裸connection、SQL callback或detached audit identity。一个`BEGIN IMMEDIATE`内从raw数据重证install generic/specialised receipt、V12 closure、`read_only` runtime revision 1与空post-install suffix；确认head为空后，唯一写入是一条fixed `INSERT` initial head。COMMIT返回不等于可发布成功，必须用新连接fresh reconcile同一canonical row并通过storage durability barrier；该适配器仍为production-zero，不翻file authority、不写后续阶段；
  - durable source freeze 只能在严格 catalog EX → repository EX 租约和 exact terminal snapshot下安装，marker绑定 authority epoch、source revision、runtime generation、code digest及独立摘要。它一旦耐久，所有普通 legacy lease/读写/扫描/复制在 I/O 前失败；只有同一 private cutover recovery seam可继续，后续 admission CAS与SQLite authority lease都必须重证 marker；
  - asset stage 的 project/blob/occurrence统一投影为 `member_kind / ordinal / member_id / member_bytes / member_sha256 / metadata_sha256`；identity/value/metadata与三类成员分别 domain-separated，manifest受预算约束，ordinal按 kind稠密。StagePlan冻结这组值，未来 SQL writer/reader不得各造一套 ID 或摘要算法；
  - mount protocol v2 把 install 与 coverage 的 generic parent `(transaction id, kind, committed_at)` 都放入 canonical manifest，并强制完整耐久顺序 `coverage ≤ install ≤ audit ≤ activation ≤ mount`。cutover head再按 `installed → assets_covered → audited → activated → mounted` 形成五态 hash chain：phase 2在 install时纳入已经提交的coverage，不表示coverage此时才发生。strict production authority reader从最终 mount重建全链并逐列比对；裸 evidence、旧 observation、v1 mount、乱序时间或等长 manifest/member/head 篡改都不能授权；
  - production outer plan 把 fresh mount、全部 worker target/genesis 与全局 outbox allocation 绑定到 `AtomicBusinessWritePlan`。3.18.0 的历史第一条 fixed-SQL 垂直只接受 `0 pipeline control + 0 durable action + 1 outbox`；3.23.0 已在不改 schema 的前提下扩为 pipeline/action/multi-outbox 通用 writer，并保持同一 `BEGIN IMMEDIATE`、head-last CAS、pre-COMMIT 全回滚、exact retry、ack-loss fresh 恢复和五态分类；
  - `AtomicBusinessWritePlan` 把每条 `PipelineIntent` 与且仅与一个 `PipelineBusinessControlPlan` 对齐，并要求 specialised receipts 在 shared worker ledger 上连续；漏控制、多控制、换目标、换 receipt 或断 predecessor 都会拒绝。worker v3 的 receipt 绑定完整前后状态和 ledger tail，历史 receipt 需要 membership 证明；outbox `published` 与 `inflight` 分离，UNKNOWN 不得推进游标；
  - 首次 claim 必须占用 published 的直接后继；RETRY 保留同一 inflight；PUBLISHED 或有强 effect/terminal 证明的 DEAD 才能推进 published。另有一条严格的无副作用终局：租约确已过期、零 effect/result、attempt 恰好耗尽时，`EXPIRE→DEAD` 可推进；任意无证明 DEAD、跳号、替换 inflight、no-op hash churn 或 receipt 复用都拒绝；
  - `worker_result_projection_contract.py / worker_result_projection_integrity.py` 已把 SQL writer/auditor 必须从 sealed `WorkerExecutionPlan` 写入并重建的原始 payload bytes 冻结为纯投影：`AUTHORIZE_EFFECT` 只产生独立 effect-request projection；每个 `COMPLETE / RECONCILE` 都产生 result projection；`RECONCILE` 必有 proof，`COMPLETE` 只有实际携带 effect link、durable result 或 terminal proof 时才有 proof。`EXPIRE` 故意不伪造 result/proof；effect request、顶层 result/error、proof evidence/provider receipt/durable result 各归唯一 carrier 所有；
  - 投影 metadata 不复制嵌套 payload；摘要按 role 加上 `schema / codec id / codec version / exact bytes / payload sha256` 逐段做 uint64 length framing。durable result 已有 id 时沿用，否则 result/proof id 分别用独立 domain 从 request、target、operation、after status 确定生成。V8 的 `worker_results.proof_sha256 NOT NULL` 在“真实无 proof”时只能使用兼容哨兵 `f14fd90bc57f6d54c068aff36091f91a2853b2b4ffa48c148a3a46d8854a3a47`；纯投影仍保持 `None`，该哨兵被保留且不能冒充真实 proof；
  - `SQLiteWorkerClaimWriter` 是第二条、但仍然极窄的 fixed-SQL 垂直：只接受 worker ledger genesis 上的首个 `OUTBOX / CLAIM / PENDING revision 1 → LEASED revision 2 / attempt 1`。一个 `BEGIN IMMEDIATE` 同笔 INSERT generic receipt、specialised receipt、ledger entry、attempt，并 CAS worker current、legacy `event_outbox` 镜像、dispatch head 与 ledger head；它写入的 effect/result/proof 数量严格为零。租约和 occurred-at 以同一 SQLite 连接/事务内读取的数据库时间判断，并在 COMMIT 紧前再次校时；COMMIT 后必须 fresh reconcile 与 storage durability barrier；
  - claim reconcile 不凭“行存在”猜成功：会从 canonical pure atomic manifest 重建完整 atomic receipt/root 与 event immutable row，再闭合 origin outer target、genesis/current、allocation、dispatch、worker receipt 与 ledger；pre-COMMIT 可证明全回滚才是 `NOT_COMMITTED`，ack-loss 可 fresh 恢复，identity/head 漂移为 conflict，部分/等长篡改为 corrupt，BUSY/I/O/耐久屏障失败为 unknown。后续纯业务 commit 不抹掉旧 claim；shared ledger 后缀若全是 auditor 支持的 OUTBOX `AUTHORIZE_EFFECT / COMPLETE / RECONCILE`，旧 claim 可以通过最小 durable identity 重联为 exact；遇到 `RENEW / EXPIRE`、后续 retry claim、pipeline/action worker execution或超出当地审计预算的合法形状则保持 UNKNOWN，不误报 CORRUPT；
  - `SQLiteWorkerProjectionWriter` 是另一条窄 fixed-SQL 垂直：只接收同一 OUTBOX 的 issuer-sealed `AUTHORIZE_EFFECT / COMPLETE / RECONCILE` plan 与对应 sealed projection；一个 `BEGIN IMMEDIATE` 先写 generic/specialised receipt、ledger member、V8 effect/result/proof mirror 和 V9 payload sidecar，再 CAS legacy `event_outbox`、`worker_target_current`、必要的 dispatch head，最后才推进 worker ledger head。任一 projection 缺失、多出、与 plan 错配或 pre-COMMIT/fresh reconcile 无法闭合都不得报 committed；
  - `SQLiteWorkerDurableAuditor` 只接收 request id 和可选的最小期望 identity，不接收 `WorkerExecutionPlan`。它在一个 pinned read snapshot 内 bounded decode 耐久 manifest/sidecar，重建并重验受支持的纯 worker/projection plan，闭合 shared worker ledger、multi-allocation batch、V8/V9 mirror、per-target state/current、legacy event 与 dispatch/head，再用第二个 fresh snapshot 做 publish fence；`EXACT / ABSENT` 发布前还必须通过统一 storage durability barrier。3.23.0 已能从 persisted intent/receipt/transition/target 重建 pipeline-control 完整语义并参与 EXACT；尚未支持的 ACTION/PIPELINE worker execution、renew/expire/retry 继续返回 UNKNOWN。查询结果只有 `EXACT / ABSENT / CONFLICT / CORRUPT / UNKNOWN`。
  - migration ledger 与 generic receipt namespace 各只有一套 domain-separated、length-framed canonical root。receipt 按 UTF-8 SQLite `BINARY` 顺序流式审计，kernel 显式拒绝 UTF-16 数据库；full audit 在调用方持有的同一 transaction snapshot 内返回 schema fingerprint、migration root 与 receipt root，certificate adapter 不得在函数返回后另开 snapshot 拼证据；
  - Document 只向 exact private capability + exclusive mutation session 签发一次 opaque candidate，先把内存 version/timestamp 精确推进一格；确定未提交时能原位 rollback，只有受控 private writer 的 exact committed 结果才能关闭 session。candidate 不写文件、不证明 durability，也不进入稳定 SDK；
  - deterministic snapshot codec 只接收严格 JSON 值（正负 int 的十进制数字最多 4300 位，符号不计），输出唯一 UTF-8 bytes 与 SHA-256，并以严格 decoder 证明无损逆变换；同一精确类型和值图不受 dict 插入顺序影响；
  - bounded codec 在正常 encode/`json.loads` 前先按 canonical byte 与“value + object key”节点预算增量预检；snapshot 每行统一使用 byte ceiling + 250,000 node ceiling，巨型 string/容器不会先完整分配再报超限；
  - `payload_sha256` 只对传入的精确 bytes 求摘要，不会把任意 bytes “认证”为合法快照；canonical 与 digest 是两项都必须成立的独立事实。snapshot verifier 在 byte ceiling 后先重算并比对 hash，再进入 bounded canonical decode/node scan；
  - `business_snapshot.py` 把锁内的一个 Document 投影为 project/document/entity 三类规范行；每行绑定 codec/format、列值、canonical bytes 与 32-byte SHA-256，Entity 另保存每 type 插入位次；project root 用 domain-separated uint64 length framing 流式绑定按 identity 排序的完整 Entity 集合；
  - codec 的 128 层是为 snapshot 包装层留余量的线格式上限，不是业务值域；verify 还会用 Entity/Document 的无 hook 所有权克隆重证 data schema/config/ACL、Entity data、AiProvenance 与整个 actor log 均处于 120 层/250,000 展开节点业务域，不允许“verify 成功但 restore 构造失败”；
  - `verify_business_snapshot` 分阶段验收：先预检 project/document carrier 与 byte 上限，再按 codec 元数据 → hash → bounded canonical decode（node scanner 在 `json.loads` 前）→ 闭集/业务域 → 列绑定验收两行；根的 Document descriptor/count 一致后，才预检全部 Entity carrier/总 bytes，逐行验收 identity 顺序、每 type 连续位次并最后比对 root；它能检测部分改写，不是带密钥真实性：可一致重写全部 leaves+root 的攻击者仍需后续 receipt 外部锚/签名边界；
  - capture 包含软删 Entity、不包含已 hard-remove 的 Entity，并把 read-only overlay 排除为显式 diagnostics；它不会用 `deepcopy` 执行扩展对象钩子；
  - restore 在全量验证后按保存位次重建 Document/Entity，不经 `Document.open` / `Entity.from_dict` / `Document.add`；恢复后挂接可证明的 framework schema validator、不重验历史数据，并立即重新 capture 比对每一行与 bytes；返回头固定标记 `requires_runtime_rehydration=True`，在唯一 seam 重装 harvest/read-only/runtime ports 前不得直接服务；
  - restore head 的签发事实与原始 business root 留在模块 issuance ledger，不把可复制 seal 放在 carrier 上；`detach_restored_business_head` 在交接时重新 capture、精确比对并一次性消费，因此 restore 后发生的未持久化 mutation 不能伪装成 runtime no-op；
  - runtime rehydration 套件按 state / projection / coordinator 三层单向依赖；`runtime_rehydration.py` 门面是唯一同步 load coordinator：同一线程、同一未发布 Document 的 exclusive mutation session 内完成 baseline 重验、当前 registry 声明冻结、canonical migration、changed/new 行最终 schema 校验、一次且仅一次 Document version 推进、可选 authority CAS、commit 后只读 overlay 物化和一次性 ready admission；中间 candidate、proof 和 mutable Document 均不对普通调用方暴露；
  - canonical 无变化时不调用 authority writer、不推进版本；有变化时 writer 必须接收 `before generation/root/document_version` 与冻结 after snapshot，并返回精确 `committed / not_durable / unknown` typed receipt。`not_durable` 才回滚，`committed` 后永不回滚，`unknown`、receipt 错配、rollback failure 或 commit 后 overlay failure 一律 quarantine；
  - read-only source 在 canonical authority 成功之后才解析和物化，migration 不能依赖外部即时 overlay 生成持久化结果；全部 overlay 完成后还必须用 `exact_business_head_fence_v1` fresh-read fence 重证 project/generation/root/version/declaration digest/runtime generation 当前，覆盖 no-op load、commit→overlay 期间另一进程推进 head，以及滚动部署 worker 使用不同 collaboration units/harvest/overlay manifest 的窗口；
  - coordinator 不向外返回可延迟持有的 pre-admission ready handle；final fence、session close 与 admission 签发在同一次同步调用内完成，直接返回 `RuntimeAdmission`。Admission 同时携带 Document、完整 declaration manifest（含 collaboration units）、authority revision/root/version，未来 Context/CRDT/commands/supervisor 不得旁路再抓另一份 registry；
  - strict scanner 只接收调用方明确点名的 live topology project 与 expected generation，不遍历 repository catalog。外层同时固定 topology generation 与 live-root inode，内层再持 Document content SH；同一个嵌套 context 内完成 exact head/Entity 读取、business snapshot capture 与二次 verify，退出锁域后不再拿路径补读；
  - scan handle 只暴露 project/document identity、generation/version、storage version、数量/字节数与摘要；不暴露 `Document`、snapshot rows 或业务 payload。read-only overlay 仍按 snapshot 合同显式排除，不能被悄悄算作 canonical 业务数据；
  - prepare 将明确选择的 handles 按 project id 规范排序，以共享的 `uint64-BE length || bytes` framing 流式计算虚拟 payload；它不会把最多 1 GiB 的行拼成第二份巨型 bytes。source head 必须非空、跨项目 Document id 不得重复，避免签发 SQLite schema 必然拒绝的输入。canonical manifest 明写 `selection=explicit_projects`、每项目 source head/business root/counts 与整批摘要，因此证明的是“这份选择”，不是“整个 catalog 已扫描完整”，也不是“所有项目在同一全局时点的原子快照”；
  - prepared package 与 `prepared_verified_payload` receipt 由同一进程 identity ledger 成对签发并绑定 import id、manifest/payload/batch 摘要。这个 receipt 只证明“内存中的选择已规范准备并重新验收”，**绝不证明 commit、durability、SQLite 落库或 authority**，也不能转换成业务 `CommitReceipt`；
  - ledger 为 scan/prepared record 和每份 reservation 绑定签发 PID + epoch；真实 fork 后子进程直接替换继承的 `RLock` 与全部 identity ledger，不触碰可能由其他父线程持有的旧锁。继承对象即使仍能被同进程可信 Python 内省，也不能经受支持的 carrier/ledger seam 读取属性、取得 verified snapshots、release 或 finish；父进程的副本继续有效，子进程若要工作必须重新扫描和 prepare；
  - 每个 scan handle 有两条彼此独立的一次性 lane：payload lane 只用于组装一批 prepared package；file-authority runtime compatibility lane 只把同一已验证 snapshot 恢复后交给既有 runtime rehydration。消费其中一条不会替另一条背书，也不会让另一条可重复；
  - prepared package 的 private adapter seam 是 `reserve/recover → verify → INSERT-only transaction → fresh reconcile + durability barrier → verify → explicit decision`：只有逐行 exact `IMPORTED` 且数据库文件/WAL sidecar 安全复检、父目录 fsync 同时通过，才映射 `consumed`；精确 `NOT_IMPORTED→release`，冲突/损坏/未知→quarantine。写入尝试一旦开始，到 fresh 分类完成前的异步中断一律按未决写入 quarantine，绝不把可能已经落库的同一 package 释放给另一目标；只有确定尚未进入写入区间的取消才可 release。`consumed` 仍只表示进程内 lane 已终结；真正的导入结论来自新连接逐行核对 exact plan 与 kernel 统一耐久屏障，而不是异常或 generic receipt。active reservation 由强 identity registry 持有，finish 的 exact 同 outcome 在返回确认丢失后可幂等重试，换 outcome 固定拒绝；该 bridge 不进公开面，也没有 bootstrap/runtime caller；
  - reserve 现在从 sealed record 原子投影完整 opaque `VerifiedLegacyImportPlan`：先在 ledger lock 内结构克隆整棵行图，锁外重建并验证 canonical payload/manifest/root，再回锁逐字段复核当前 sealed graph 并签发。final 回锁确认不再额外 clone 完整 snapshot 图；峰值仍诚实包含 sealed graph、isolated projection graph 与必要的 plan row-intent graph。plan 与原 record 不共享 Project/Document/Entity row 对象。每个项目直接暴露无 `__dict__`、不可改写的 project/document/entity SQL row views；`snapshot` 仅是兼容性 detached copy，调用方即使改坏它也不会改变 plan-owned rows，private importer 禁止把 detached snapshot 当权威写入输入。plan 同时带 plan/import/receipt intent、manifest/batch/payload protocol、selection、全批 counts/bytes、manifest bytes 与三摘要；每个连续 ordinal descriptor 带 project/generation、Document identity/version、source/storage/business root/counts/bytes 和 verified row intent。importer 已无需且禁止读取 package properties 或 `reservation.record` 拼装输入；
  - plan 无 `__dict__`、不能直接构造/复制/序列化，也不进入公开 `sdk.__all__`；ledger 把它与 exact active reservation 按对象 identity 一一绑定，private importer 在写前与对账后都必须通过 pair-binding recheck，不能提交伪 plan、另一份 plan 或 source 已漂移的旧 pair。reserve 仍保持单赢家；调用方必须在 reserve **之前**自持一个 exact process-local attempt token，recovery 同时核对 package、receipt 与该 token：plan 已签发时返回同一 pair，底层 reserve 返回确认丢失且尚未签发 plan 时就在原 reservation 上继续完成投影和绑定；若 reserve 的 release/quarantine cleanup 自己被中断，exact recovery 会先完成 pending outcome。仅拿相同 package/receipt 的并发者不能窃取 active pair；若 exact owner recovery 时发现 sealed source/issued state 已坏，ledger 会在报错前原子 quarantine，不能留下无人持有 reservation 的永久卡死 lane；
  - plan 不是 currentness/catalog completeness/authority proof；private importer 会在同一事务写三类业务 rows、terminal ledger 与 exact receipt，并在 COMMIT 后用新连接做 adapter-specific exact reconcile，再走与 generic kernel commit 共用的 storage-file 检查和 parent-directory fsync。屏障失败只能归类 UNKNOWN，不能因 receipt 可见就报告成功。plan 不产生 `committed_at`，也不能单独决定 consumed/release/quarantine；
  - importer 只使用七条精确 allowlist 内的 plain INSERT；这一层拒绝 REPLACE/upsert/拼接 SQL，专用 authorizer 再拒绝非白名单表、直接 UPDATE/DELETE、DDL、PRAGMA、ATTACH 与事务控制。v3 时 kernel 只接受 `EMPTY` 或 marker 标记的结构闭合 `TERMINAL_SHADOW`；v4 再增加 empty/nonempty historical seal 两个闭合终态。同 id 同内容精确幂等并保留原 `committed_at`，同 id 异内容或跨 id 目标重叠为 conflict；历史封印出现前仍可追加互不重叠批次，封印后只允许 exact retry，不能再扩大目标。逐批 reconcile 流式比较，不 `fetchall` 全部 Entity；
  - topology 的 catalog migration barrier 是全 repository 的写入准入层：普通 lifecycle/activity 先持 SH，唯一严格迁移入口按 barrier EX → repository EX 枚举完整 live namespace，并把两轮扫描、SQLite 导入、封印与最终 fresh reconcile 都留在同一屏障内。严格枚举拒绝未决 journal/recovery、非空 staging/trash、孤立或缺失 root、非终态/错路径 state、marker/generation/Document identity 漂移；fork child 只关闭继承 fd，不能用 `LOCK_UN` 解除父进程锁；
  - private catalog coordinator 对非空目录按 descriptor 内容派生稳定 import id：第一轮准备并写 terminal shadow，第二轮重新扫描、重建独立 plan，只有 descriptors 完全相等才写 seal。空目录不创建 scan/prepare/import lane，直接封印 empty catalog。sealer 只执行 marker、receipt、seal、members 四类固定 INSERT，生产 API 不接受 callback/connection；
  - importer 与 sealer 共用一个 byte-exact catalog projection/reconcile 真相源。封印后，importer 的正向结果也必须精确比对 seal/member/receipt；即使 manifest 或 digest 做等长改写，也不能出现一边报告成功、另一边报告损坏。两条 bridge 的 COMMIT/close/fsync acknowledgement loss 与异步中断都先 fresh 分类，再按 imported/sealed、not committed、conflict/corrupt/unknown 决定 consumed/release/quarantine；
  - Entity row carrier 的 tuple subclass 只在入口出现一次：容器用 base `tuple.__iter__` 冻结引用，字段名确认后所有值均以 base `tuple.__len__/__getitem__` 读取，禁止可覆写 property/iterator 让 preflight、integrity、DML 与 reconcile 看到不同内容。临时 normalized business graph 仍按 project 释放，长期 plan 只多一份 row 引用 tuple，不恢复第二份全批 Entity 对象图；
  - 当前 plan issuance 仍有整批 O(n) 结构 clone + ledger-lock 内最终 identity scan；importer 消除了第二份全批 Entity projection 和 payload 拼接，但这不代表百万 Entity 的峰值 RSS、锁尾延迟和导入时长已达生产标准。自动迁移上线前仍须增加最大批 RSS、P95/P99 lock wait/transaction duration 压测门禁；
  - 进程退出会丢失 process-local prepared issuance ledger、未决 reservation、plan 与未消费包，恢复方式是从仍为 authority 的文件源重扫；已经提交的 terminal batch/descriptor/receipt 则可跨进程 fresh reconcile，同一 exact import 不会重复落库；
  - codec 本身仍不认识业务模型；snapshot 合同虽认识 Document/Entity，但自身不扫描文件、不连接 SQLite、不消费 OpLog，也不导入 sdk/topology。当前 `user_version=13`，普通初始化的 `kernel_repository.authority` 始终为 `legacy`；V1–V12 全部是冻结历史前缀，V13只追加 4 张 direct-predecessor binding 私有表，普通 unit of work 无权写；
  - kernel receipt 只证明 SQLite unit of work，prepared receipt 只证明本进程载荷准备，terminal import receipt 只证明显式批次已精确落入影子，catalog receipt 只证明严格屏障持有的完成时点覆盖了当时全部 **live business** 项目。generic UnitOfWork 是无字段 opaque handle，connection 只在 closure identity ledger 中绑定到 callback 返回，executor 只交付物化结果；随后立即 revoke，fork child会替换继承 ledger。EMPTY 时 authorizer 拒绝读取十一张 private 表；terminal/catalog marker 建立后 generic commit 在 callback 前整体关闭。上述证据都不能映射成业务 `CommitReceipt`；catalog seal 刻意不含 tombstone、OpLog、assets、inbox/outbox/action，也不在屏障释放后持续 current，更不等于跨进程服务、离线同步、跨设备保存或 authority cutover。

## 怎么用

```python
store = FileCommitStore(project_dir / "document.json", op_log)
# coordinator 是 project-scoped 单例；所有 surface 必须拿同一实例/同一把锁。
coordinator = CommitCoordinator(
    document,
    store,
    publisher=BrokerEventPublisher(broker),
    lock=project_commit_lock,
    lifecycle_guard=lambda: topology.activity(project_handle),
)

# producer 内调用旧 registry 时必须关闭其 op_log / broker 副作用，
# 只让它执行校验与内存 mutation，并返回 Operation。
execution = coordinator.execute(
    lambda: command_registry.dispatch(
        "entry.update",
        args,
        actor=actor,
        source="user",
        doc=document,
        op_log=None,
    )
)
```

需要启动 pipeline 时，handler 返回一笔显式计划，而不是在 producer 里调用另一个 coordinator：

```python
return CommandMutation(
    operations=(start_op, *placeholder_ops),
    after_commit=(DeferredAction(
        id=f"pipeline.start:{run_id}",
        name="pipeline.start",
        run=lambda commit_id: activate_run(),
    ),),
)
```

调用面使用 `execution.receipt` 回应保存状态，使用 `execution.events` 做协议响应或测试；不能再自行 `doc.save`、`op_log.append` 或重新运行 `affected_entities`。未配置 publisher 时的 events 也不授权 surface 在解锁后自行广播；生产实时路径必须把 publisher 注入 coordinator。

## 明确限制

FileCommitStore 是旧文件格式的诚实兼容层，不是假装成数据库事务。Document 与 NDJSON 之间仍可能发生半提交，所以 receipt 必须保留 `indeterminate`。after-commit action 目前也还没有 durable action journal：进程若在业务提交后、action 执行前退出，未来恢复器应按 action identity 补跑，而不是重放命令。W06.2g 把明确 plan 写成 terminal shadow；W06.2h 用全 repository 屏障和 v4 seal 证明完整 live catalog；V5a.1 又在同一屏障中把 live、deleted tombstone 与 generation high-water 写成可逐字节 fresh reconcile 的完整历史 proof。它仍不是持续 current 的生产数据库：OpLog、assets、inbox/outbox/confirmation/action 不在同一业务事务，屏障释放后文件 authority 可以继续推进。在V5 proof、preinstall与普通启动路径中，`kernel_repository.authority`仍保持`legacy`；3.27.0的显式private install可在自己的SQLite事务内把数据库marker CAS为`sqlite`并建立`read_only` runtime，但不执行文件admission CAS、不切production router，线上读写仍完全走文件。禁止把数据库内部安装marker或exact proof描述成“业务已迁库”，也禁止长期dual-write。

Runtime migration 与 loader 目前仍是同进程可信 Python callback。exclusive session 已禁止它们调用 live `Document.save`，但框架仍不能回滚 callback 私自进行的其他网络/文件副作用，也尚无稳定 migration id/code digest/applied receipt、overlay 总行数/总字节/耗时预算。生产接线前必须把调用限制在 topology generation lease + per-project singleflight + 未发布 Document 独占区间，并由后续 production coordinator 在 private importer 外补齐 payload-bound CAS 与最终 fresh authority fence。Context builder 还必须原子 claim 一份 Admission（禁止一包建多个 Context），从 `authority_revision` 建 outbox/订阅并追平后才服务；quarantine 也需要耐久 repair owner/metrics，当前只保证不误发布。同进程恶意 Python 不属于对象 seal/issuance ledger 的安全承诺，需用进程隔离解决。

数据库当前 `user_version=13`，这是单向升级边界。V1–V12 的 checksum/DDL 与历史语义都不可改；V11→V12只接受authentic empty V11，V12→V13又只接受clean/authentic、legacy、production-empty V12。历史 mounted/active 库不满足这些clean upgrade前置；不得清表、改 `user_version` 或重签摘要绕过，真实迁移必须另立导出/重建或备份恢复协议。旧二进制会对超前 schema fail closed，回退只能恢复升级前 SQLite backup。现在已有 strict fresh mount reader、通用 production writer/reader、V12 install closure、private install writer、phase-1 finalizer、plan-free durable auditor、V13四类direct-predecessor carrier/guard/fresh语义reader，以及第一段private asset-provider fixed-SQL writer。V13已能逐字段证明binding与canonical head链，但audit/activation/mount三个fixed-SQL writer、durable coordinator、authority router、生产caller和持续dispatcher仍不存在。zero-control历史commit仍缺提交时scheduler checkpoint；atomic-v1仍是一条operation对应一条event；历史版本化snapshot、完整worker执行、renew/retry/expire与长期authenticated checkpoint/accumulator仍缺失。进入source freeze或`cutover_pending`后只能向前恢复；current V13私有闭环全绿不等于已经可切生产，更不等于具备离线/跨设备同步。

完整不变式和失败矩阵见 [SPEC.md](./SPEC.md)；SQLite 的当前边界、运维纪律与 cutover 清单见 [SQLITE-KERNEL.md](./SQLITE-KERNEL.md)。
