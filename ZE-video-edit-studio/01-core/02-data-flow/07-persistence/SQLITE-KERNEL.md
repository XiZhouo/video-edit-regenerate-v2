# SQLite 事务 Kernel、Legacy Shadow 与私有原子业务读写（W06.1–core 3.31.0 / schema-v13）

这层先把“同机多进程怎样安全地打开一个事务库、怎样在崩溃后证明一笔提交到底有没有成立”做成可复用内核。V1–V12 已形成冻结的历史前缀：V12 用 4 张表补齐 member mapping 分块 carrier 与 pre-mount baseline outer sidecar；3.27.0 增加只消费 exact closure projection 的 private fixed-SQL install writer，3.28.0 增加从数据库自身重建六个恢复点的 private plan-free cutover durable auditor，3.29.0 在不改 V12 identity 的前提下增加 database-only + capability 的 private phase-1 finalizer。current schema-v13 又用 4 张 append-only 表补齐 asset-provider、audit、activation、mount 四类 direct-predecessor binding，并由 `sqlite_cutover_phase_binding_reader.py` 从原始行逐字段重建纯合同、canonical manifest 与完整 head 链。3.31.0再增加第一段可执行的private asset-provider fixed-SQL writer；当前缺少的是audit、activation、mount三个后续阶段writer以及production coordinator/router/dispatcher。**生产 coordinator/router/dispatcher 仍未消费这些私有适配器，所以当前线上 Document、Operation、事件或 Pipeline 的事实源仍是旧文件路径**。

## 1. 当前交付到了哪里

W06.1–core 3.31.0 在 **POSIX 同机本地文件系统**上交付 SQLite/WAL kernel、schema registry、纯 codec、业务快照与 legacy 迁移证明、冻结的 v6 原子事务底座、schema-v7 local revision 修复、历史 V8 authority/worker、历史 V9 payload sidecar、历史 V10 runtime hardening、历史 V11 mount chronology closure、历史 V12 install closure carrier、current V13 direct-predecessor bindings、private fixed-SQL install writer、global cutover durable auditor、private phase-1 finalizer、asset-provider phase writer与strict fresh readers，负责：

1. 以固定安全基线创建和打开数据库；
2. 串行、可校验地执行 schema migration；
3. 在调用方的 SQLite unit of work 内记录最小 committed receipt；
4. 用新连接按稳定 transaction id 做 fresh reconcile；
5. 用独立进程和真实崩溃点证明：提交成功但响应丢失时可找回，事务提交前退出时不会伪造成功。
6. 以 v2 migration 建立空的 `projects` / `documents` / `entities` STRICT 表，作为后续规范业务快照的固定落点；
7. 把严格 JSON 值编码为唯一 UTF-8 bytes，严格拒绝 alternate representation，并对精确 bytes 计算 SHA-256；
8. 把一个已持锁 Document 投影为 project/document/entity 规范行集，以 project root 绑定完整 Entity 集合，并在纯内存中验证与恢复；
9. 对调用方明确点名的 live topology project，在 generation/root/content 共锁内 capture+verify，把多个 scan handle 规范准备成 identity-bound 的本进程虚拟 payload/manifest，并原子投影完整 immutable import row-intent plan。
10. 以 v3 migration 建立 transaction receipt exact binding index，以及 `legacy_import_batches` / `legacy_import_projects` STRICT 历史账本，为 committed import 提供不可含糊的耐久落点。
11. 用固定 INSERT-only SQL 在一个事务写入 exact business rows、terminal batch/descriptors 与 receipt；再从 fresh connection 对 exact plan 逐行流式 reconcile，并通过 kernel 唯一的 storage-file/WAL-sidecar 复检与 parent-directory fsync 屏障后才报告成功，覆盖 COMMIT/finish 返回确认丢失、精确幂等、冲突、损坏与耐久未知分类。
12. 以 v4 migration 建立 append-only `legacy_catalog_seals` / `legacy_catalog_members`，在严格 repository barrier 内枚举全部 live business project、完成两轮独立扫描，并把 empty/nonempty catalog 的 exact historical seal 与 terminal shadow 逐字节闭合。
13. 以 V5a durable admission 在任何 legacy repository/project/Document I/O 前拦截 pending/sqlite writer，用 prepared/record/committed 安装记录跨崩溃恢复；以 v5 migration 预留三张 append-only repository proof 表，能表达 live+deleted generation high-water 全集，但固定 `authority=legacy / cutover_ready=0`。
14. 在同一 repository barrier 的 S0/S1/S2 之间，用 fixed-INSERT writer 与 fresh byte-exact reconcile 把 v4 catalog、live Document head、deleted tombstone/high-water、runtime/code identity 和完整 receipt namespace闭合成 v5 历史 proof；当前仍不挂载生产或推进 admission。
15. 以 v6 migration 追加 17 张 `STRICT` 私有事务表，覆盖 runtime control、current project/head、commit/Operation/target、inbox/outbox、confirmation、durable action、pipeline job 与外部内容寻址 asset metadata/ref；v1–v5 DDL/checksum 保持冻结，已发布 v6 checksum 固定为 `41fe4ba13a35bacfa75af648381c0dadc4d256a8872c65a1bbf0138b64252254`。
16. 用 immutable `AtomicCommitPlan` 闭合 source admission、global/project/head 三 revision、before/after head、完整 snapshot、Operation hash chain/targets、outbox、可序列化 action/pipeline、asset occurrence、inbox 与 confirmation resolution，并在任何 writer 消费前重建全部 manifest/摘要；change manifest 的 protocol/schema/hash domain 因 shape 改变显式升 v2，baseline v1 使用独立 hash domain。
17. 只向 exact exclusive mutation session 签发一次 opaque Document candidate；未提交可恢复原对象图，只有 private writer 的 exact committed 结果才能关闭 session。
18. 以 checksum=`d96606b2d6b47ee42e53dd216e7c711e00c877573814c4cc73526004e8aa0656` 的 v7 migration 在不改写 v6 的前提下分离 global repository、per-project 与 per-head revision；对旧 prototype 数据先做单项目连续历史 preflight，再回填 local revision、重建 head FK 与 closure trigger，不可证明的数据整笔拒绝。`repository_runtime_control.schema_version` 是 v6 runtime-control 行格式版本，V8–V13 后仍固定为 `6`；数据库 current 状态由 `PRAGMA user_version=13` 与 exact schema fingerprint 证明。
19. 用只读 runtime validator、fixed-SQL `BEGIN IMMEDIATE` writer、fresh exact reconcile 与 rooted reader 真实执行同事务提交、ack-loss/process-control/durability 分类、完整 member closure 和 pinned-snapshot 线性化；这些模块无 production caller、activation API 或 SDK export。
20. 将文件 admission 收紧为不可逆的 `legacy R → pending R+1 → sqlite R+2`，并在任何cutover shadow写入前于严格EX租约安装durable source freeze。freeze绑定epoch/source revision/runtime/code且永久阻断普通legacy SH lease与I/O；每条CAS及最终SQLite authority lease都必须重证marker，freeze/pending后只能向前恢复。
21. 用 pure issuer-stamped cutover plan 绑定 V5 proof、三份 admission digest、live baseline/deleted high-water mapping、read-only install 与 active join；用独立 pure worker plan 绑定 pipeline/action/outbox 的 lease/fence/attempt/effect/terminal 状态事实。cutover 仍无 durable coordinator/schema writer；通用业务 writer已能落 pipeline/action/outbox genesis，但 worker execution仍只有首次 CLAIM 与三种 OUTBOX payload transition，两者都没有生产 caller。五份 cutover 事实不等于五阶段已经耐久发生，`expected_target` 也不等于 current registry 证明，后续 coordinator 必须用分事务前驱 receipt/CAS、fresh reconcile 与同事务 schema registry 校验补齐。
22. 把 runtime admission 拆为无历史扫描的 transaction fast fence 与显式 startup/periodic full audit；后者负责 page integrity、全 FK、receipt-history、global/project/head 连续 revision、live current projection 与 final fresh publish closure。空历史只能是 repository revision 1；full audit 重复采样 main-file inode，但 production 仍必须持外部 repository/maintenance lease 防恶意反复 ABA。
23. 用 bounded asset-stage plan 逐字节验证 blob长度/SHA、canonical metadata与所有occurrence，再由durable-reader盖章的exact stage observation组成repository coverage；project/blob/occurrence同时投影为唯一canonical SQL member六元组，kind/purpose分域、ordinal按kind稠密。跨项目同SHA只计一次物理字节，live/deleted/zero-asset member与聚合预算全部闭合；plan或裸evidence不等于持久化。
24. 用独立 full-audit adapter、durable reader 与 ledger reader capability 形成 append-only audit certificate 纯合同；singleton head、generic receipt、specialised certificate 与 predecessor/tail 每步闭合。历史 proof 目前限制 256 hop/16 MiB，超限未来必须返回 UNKNOWN，长期需 checkpoint/skip-link/Merkle accumulator。
25. 用 activation v2 把 fresh final admission、exact read-only install、fresh INSTALL audit、真实目标 migration-ledger count/root 与 `read_only state 1 → active state 2` 绑定；历史 audit 只允许恢复已提交 activation，不能授权首次 apply。v1 预生成 join 不再是 v2 的生产授权输入。
26. worker v2 增加 retry time fence、outbox sequence/cursor fence、sticky cancellation 与 typed cancel acknowledgement；pipeline create/pause/resume/cancel 改用独立 business-control issuer，不能制造 worker attempt/effect/terminal。这些值现在已有 schema-v8 耐久形状，但仍没有一般 dispatcher/worker consumer。
27. 用最终mount组合器同时重验cutover-v1 read-only install、fresh exact asset coverage与fresh exact activation-v2；receipt时间必须满足`coverage ≤ install ≤ audit ≤ activation ≤ mount`。五阶段canonical head把`installed → assets_covered → audited → activated → mounted`逐态链接，fresh reader从mount重建全链。v1 join永不授权production writer，任何非UNKNOWN分类都重新读取durable prerequisites。
28. 用 `AtomicBusinessWritePlan` 把每条 `PipelineIntent` 与唯一 `PipelineBusinessControlPlan`、specialised receipt 和 shared worker-ledger predecessor 一一闭合；缺项、多项、换目标、换回执或断链不能交给未来 writer。
29. worker v3 receipt 绑定完整前后状态、request/after-state digest 与 ledger tail；历史 receipt 必须有 membership 证明。outbox `published` 与 `inflight` 分离，UNKNOWN、丢失 inflight 的 RETRY 与无强证明 DEAD 都不能推进 published。
30. 冻结唯一 migration-ledger 与 generic receipt-namespace canonical root；数据库必须是 UTF-8。full audit 在调用方已持有的同一个 transaction snapshot 内返回 schema fingerprint、migration count/root 与 receipt count/root，禁止未来证书 adapter 跨 snapshot 补读。
31. 以历史 schema-v8 `production_authority_worker_runtime_v1` 追加 25 张 `STRICT` 私有表，承载 install/asset/audit/activation/mount、outer atomic receipt、worker target/current/receipt/result/proof/ledger 与 outbox allocation/dispatch。V8 migration checksum=`f6c8a8e1f2aa633a52da52a04e6b58ad11230f5093e4b883e7b63674bb8ad80b`，历史 fingerprint=`0d96ffa4e88ac3c3d3fd1ba1b6f09f11a3a2d7a0e1861f5781bf0856d7fce771`，历史 migration root=`9595ea544b5b055f71d2c93619e49b3353a5d48424951fc410b0ca2484c1834a`；三者都是 current registry 中不可改写的前缀 identity。
32. V8 preflight 只允许 pristine legacy-v7 upgrade：历史 17 张 transaction 表、runtime control 与 reserved namespace 必须为空，authority 必须仍为 legacy；失败发生在任何 V8 DDL 前并整笔回滚。历史 V8 fresh/upgrade 后 17+25 表全空，不自动切 authority。历史 V7/V8 writer 测试必须显式固定 registry，不能借 current V13 偷跑后续迁移。
33. mount-v2把install/coverage的generic parent id/kind/committed_at纳入manifest，以两份durable-reader stamped fresh observation签发，并在v8 mount row显式投影kind/time、trigger闭合specialised与generic父行。strict production authority reader在同一fresh transaction内重建全部typed evidence与原始域哈希，再签发mounted binding；outer plan把它与worker genesis和全局outbox allocation绑定。v1 mount、结构JOIN、裸evidence、旧observation、父子双改时间或等长篡改都不授权。
34. 3.18.0 的历史第一条 `SQLiteProductionAtomicWriter` 垂直只接受零 pipeline、零 durable action、一个 outbox；3.23.0 已在不改 schema 的前提下扩为 pipeline/action/multi-outbox 通用 writer。一个 `BEGIN IMMEDIATE` 写历史业务 mirror、outer receipt、全部 control/target/current/allocation并最后推进两类 head与 runtime；pre-COMMIT rollback、exact retry、ack-loss fresh恢复与 corrupt/unknown分类均已执行。它仍没有 worker dispatcher、authority router或 production caller。
35. 用纯 `worker_result_projection_contract/integrity` 从 sealed worker plan 投影 exact payload bytes：AUTHORIZE_EFFECT 只有 effect request，COMPLETE/RECONCILE 必有 result，RECONCILE 必有 proof，COMPLETE 仅在真实 effect link、durable result 或 terminal proof存在时有 proof；EXPIRE 不伪造 result/proof。metadata 与 raw payload 单一所有，摘要按 role 与 payload 五元组做流式 length framing，result/proof id确定生成且完整重验。
36. V8 的 `worker_results.proof_sha256 NOT NULL` 对真实无 proof 的 result 只允许兼容哨兵 `f14fd90bc57f6d54c068aff36091f91a2853b2b4ffa48c148a3a46d8854a3a47`；纯模型仍为 `None`，哨兵不能冒充真实 proof。首个 `SQLiteWorkerClaimWriter` 只接受 genesis worker ledger 上的 OUTBOX PENDING→LEASED attempt 1，用一个 `BEGIN IMMEDIATE` 固定写四个 INSERT surface、CAS 四个 UPDATE surface并重验 closure；effect/result/proof始终为零。
37. 以历史 schema-v9 `worker_payload_projection_sidecars_v1` 只追加 effect request、result/error、proof evidence/provider receipt/durable result 三张 `STRICT` sidecar。migration checksum=`23018e6b59b8d3ad235e3110abd122ba12584cee272cff0614730f4d4be0dba0`，历史 fingerprint=`ccb5b4d248c2c429c0a99a5811816521cf4563de3bee457db826b4e4d4090ddf`，历史 migration root=`32ade765a739bca3427b4901fa4eee6a737d9d4a0519c369edd2deea56d7b26a`，三者均为 current registry 不可改写的前缀 identity。
38. V9 preflight 只接受 pristine 或完整闭合的 claim-only / pipeline-control-only V8 历史；effect/result/proof、其他 worker operation、孤儿 reserved receipt、ledger 断链、未 headed suffix、attempt/current 漂移都会在 DDL 前整笔拒绝。三张 sidecar 的 nullable payload 五元组必须全空或全有，V9 proof pointer 保持真实 NULL；六个 preadvance guard、V8 mirror、append-only 与 head closure共同阻止漏投影、错绑定和事后补写。
39. 在历史V10 registry中，真实V8 mount可在exact V8 prefix+V9 tail+V10 hardening下继续消费且保留V8 target；这项历史兼容不等于能跨V11。V11只接受空legacy production namespace，已挂载V8/V9/V10都不得原地升级或伪造成V11 remount。DDL和trigger不会自行产生或重签payload。
40. `SQLiteWorkerProjectionWriter` 只写 OUTBOX `AUTHORIZE_EFFECT / COMPLETE / RECONCILE`，同一 `BEGIN IMMEDIATE` 内完成 sealed plan/projection 重验、generic/specialised receipt、shared ledger、V8 mirror、V9 sidecar、legacy/current/dispatch CAS 和 head-last；`SQLiteWorkerDurableAuditor` 不接收原 plan，在 pinned snapshot 中重建 OUTBOX claim/authorize/complete/reconcile 及 shared pipeline-control membership，再以 fresh publish fence 发布五态结果。受支持后缀之前的旧 claim 可恢复 exact；未支持的合法形状保持 UNKNOWN。
41. 以历史 schema-v10 `runtime_timestamp_dispatch_hardening_v1` 在冻结 V8/V9 后只追加 guard、零新表：32 个弱时间列及生产保留 receipt 父行由 34 个 timestamp trigger封闭，required 值必须为 exact 24-byte canonical 毫秒 UTC，nullable 值只能真 NULL 或同样 canonical；append-only表守 INSERT，可变 head/current 同时守 INSERT/UPDATE。
42. `outbox_dispatch_head` 的两个 tuple trigger 要求 published 只能是 `sequence=0 + pointer NULL` 或正 sequence + 完整 revision/ordinal，inflight 只能整组 NULL 或完整指向 published 的直接下一序号。V9→V10 先做全量 preflight，坏 V9 原子拒绝且不自动修复；旧 V9 reader/writer 对 V10 callback-zero fail closed。V10 checksum=`1be88f2630f37ca45d1e84ea7547b78ebc9da48385774d7e279b74721af81baa`，fingerprint=`4ef1cb22745c3982b00077762a741371adbc17ca057bf908eb18a1642a45bc5e`，migration root=`f0ebef5cc0970ae693396769326e06ac651cf96702ce5c426571464bb32392d7`。
43. historical schema-v11 `mount_chronology_state_closure_v1` 保留V1–V10前缀，只在authentic V10、legacy authority、runtime/business/28张production+sidecar表与reserved production receipt全空时重建mount/head空表。preflight在任何DDL前失败并整笔保持V10；旧V10 binary打开V11 fail closed。
44. V11 mount CHECK/FK/trigger固定`coverage_committed_at ≤ install_committed_at ≤ mounted_at`及全部typed parent；head固定`state_revision=phase_revision=1..5`、前三态read-only/后两态active、pointer按阶段存在、只许直接下一阶段且旧pointer不变。纯head issuer再把identity、typed pointers、time与predecessor纳入bounded manifest/domain hash。
45. V11 migration checksum=`48de0b2513175f8575a7127e54f684922db4eb687739e0ebbab93a0a524f54cb`，fingerprint=`d92e9a6ac8c58ef6ed598a4c53bc972239f93b8e8c2a4d8a7e54369e3dc1c74f`，migration root=`39c8bc8f749de98b64d7a89b27a2d8496f46f39e1edb3e5ff0be83599314de70`。
46. V11不含历史mounted数据库的数据迁移：任何install/stage/coverage/audit/activation/mount/worker/outbox/runtime事实都会阻止upgrade。不得清空namespace、改ledger或重签摘要绕过；保留历史实例必须另立export/rebuild、平行新库或backup restore协议。
47. historical schema-v12 `cutover_install_closure_v1` 保留 V1–V11 前缀，只在 authentic、legacy、production namespace 全空且既存 V5 source receipt 唯一、时间规范的 V11 上追加 4 张 `STRICT` 表：artifact 头、artifact chunk、baseline atomic sidecar 与 install closure。V12 checksum=`aa61960d2bd4e10a41d1ef51767bf4f4809f5fab26cae5ebef3bfa30fccf35a2`、fingerprint=`4c265275dcf7d90e4ee77fda3b3a1749646e5e55783ce17fdff40e737195b226`、migration root=`1537102bc6fe371ceef49a58b566dbd6f86940eaf838dcfeb263e3df37cbb77e`。历史 production/sidecar 28 表加这 4 表，在 V12 当时固定为 `PRODUCTION_RUNTIME_TABLES=32`；current V13 再追加 4 张 phase-binding 表，当前值为 36。
48. `member_mapping` 与 `baseline_projection` 两份 artifact 按 16 MiB 分块，每份最多 4 块/64 MiB；baseline-specific sidecar 逐列镜像 inner atomic receipt，outer 不依赖未来 mount，零 target 复用 pipeline/action/outbox 三组空根。baseline asset blob 仍为需 fresh coverage 背书的 `available` revision 1，不制造 staged revision 2。
49. install closure 以十个 exact 指针把 V5 source proof 和 V8 asset coverage 接到 artifact/baseline closure。source generic receipt 首次写入必须 canonical，之后 identity/time 永久不可改；V11 旧父回执不规范则在 V12 DDL 前原子拒绝。closure 后 36 张 materialized 表继续冻结，只有 mounted head 与 exact active runtime 同时成立才解冻。
50. shared stored-manifest parser 固定 install/coverage 的唯一解释；provenance reader 从 raw V5 control/seal/member 与 V8 stage/member 重算 source、project/blob/occurrence/coverage root并签发私有 binding；closure reader再重算 V12 chunk/artifact、baseline outer/receipt root 与 closure manifest，并将 inner 事实与既有 atomic row逐列对账。空 install 的 runtime high-water 是1，首个 post-mount commit是2；非空 install仍从1连续。full audit只从 exact closure推导偏移，且 phase1/2审计不依赖未来mount。
51. 这条 fresh read 是 startup/full-audit 级全证明，不是transaction hot path；大 carrier和全仓成员会产生线性I/O、解码与内存成本。生产接线前必须增加 authenticated checkpoint或等价小型 immutable fence，并建立数据规模性能门。
52. historical V12 migration 故意不增加 asset-provider/audit/activation/mount 四类 binding 表，也不在 schema 批次中夹带 install writer 或全局 cutover auditor；这是 V12 当时的发布边界。current V13 已用独立 migration 与 semantic reader 完整定义这些关系，仍不能由 fixture 或零散 SQL 代替。
53. core 3.27.0在V12 identity一字不改的前提下增加private install writer。真实non-empty V3 terminal shadow已拥有`projects/documents/entities`，writer只读exact核对这三表并禁止任何INSERT/UPDATE/DELETE。首条DML前，V3 import/V4 catalog/V5 source generic receipt必须与STRICT child一一对应；legacy与production prefix/kind扫描都显式CAST为TEXT，byte-identical BLOB不能藏在合法TEXT行旁。production-reserved supplement还必须恰好等于sealed stage+coverage前驱；install/join/business/worker/pipeline/audit/activation/mount孤儿及prefix/kind碰撞均拒绝，expected+extra归CORRUPT，完全不同且结构闭合的前驱归CONFLICT，非生产kernel receipt保持不变。fixed SQL冻结INSERT/UPDATE的目标列和值位次；authorizer另行限制`main`数据库、无trigger来源、固定INSERT表和唯一可更新的`kernel_repository.authority`列，并拒绝DELETE、DDL、PRAGMA、ATTACH和事务逃逸。一个`BEGIN IMMEDIATE`只写install-owned fixed DML，把数据库内部`kernel_repository.authority`从`legacy` CAS为`sqlite`并建立read-only runtime，projection时间在首写前和COMMIT前都不得晚于SQLite clock；closure最后写，随后在同一snapshot连续两次fresh语义读回，COMMIT后再用新连接reconcile与durability barrier。installed reconcile先自证stored closure及其时间，再判断请求projection是否冲突，不能让foreign request掩盖partial/future坏库。BUSY/open/I/O/semantic-read或cleanup无法取得稳定观察时只能UNKNOWN；任一race/reconcile cleanup未完成也不得发布确定态，cleanup自身再抛进程控制时仍须保留最初信号。这个数据库marker不是file-admission CAS，也不切production router。writer会物化install-owned的asset blob/ref行：identity来自baseline，MIME、extension和byte length由fresh coverage descriptor补齐；它不拥有provider/stage/coverage写入、外部lease、audit/activation/mount/coordinator/router，也没有production caller。

54. core 3.28.0在不改V12 identity的前提下增加private plan-free cutover durable auditor。唯一入口是exact database与可选detached expected；业务审计SQL只读，专用`mode=ro + query_only` opener只设置connection-local安全PRAGMA，不修复persistent journal/permission漂移。一个pinned snapshot先取单一SQLite-now上界，再重证schema/security/full audit、legacy/production receipt、V5/V8/V12 carrier、baseline当前覆盖、audit/activation/mount与canonical head；fresh snapshot随后fence文件、marker/runtime/closure/head、audit/worker/allocation/dispatch、receipt root和business high-water。状态明确分成headless `INSTALLED_CLOSURE`与`INSTALLED_HEAD / ASSETS_COVERED / AUDITED / ACTIVATED / MOUNTED`；detached结果不含writer capability。mounted零suffix仍强制重证install时项目/资产/scheduler快照，不能借historical reader跳过当前覆盖。首张INSTALL证书按已知因果后缀重建exact receipt root；排除集在canonical receipt流中以Python set核对，不拼超大`IN`参数。多audit证书逐张验证raw row、generic parent、pure predecessor、worker-tail membership和最终head，并拒绝高于当前runtime/receipt/worker head的不可能过去；没有authenticated checkpoint时仍绝不按时间戳猜后续证书的历史receipt/worker as-of，完整验证activation/mount/runtime后诚实UNKNOWN。stage-only或stage+coverage preinstall、合法post-mount suffix、周期审计suffix、逻辑预算/fence/I/O不可证明保持UNKNOWN；确定性结构损坏必须双观测，pre-mount suffix、future time、混合lineage、BLOB、carrier/head/audit/mount篡改和mounted额外row/head漂移归CORRUPT。资源门覆盖100万逻辑行、512MiB TEXT/BLOB、256张/16MiB证书、1亿SQL VM步，reserved receipt流式比较；它不冒充硬墙钟/RSS/page-count上限。外部maintenance lease、phase writer/coordinator/router与authenticated checkpoint仍不在本模块所有权内。

55. core 3.29.0仍保持exact schema-V12 identity，只增加private `sqlite_cutover_phase_head_writer.py`。`SQLiteCutoverPhaseHeadWriter`构造时要求exact `SqliteDatabase` 与`_CUTOVER_PHASE_HEAD_WRITER_CAPABILITY`，不接plan/projection、clock、裸connection、SQL callback或detached auditor identity。`finalize()`在唯一`BEGIN IMMEDIATE`内从raw rows重证install generic/specialised receipt、V12 closure、`read_only` revision-1 runtime、空head与post-install suffix；只有全部闭合才用唯一fixed `INSERT` 落revision-1 `installed / read_only` canonical head，不UPDATE任何已有事实。COMMIT后必须用新连接`reconcile()`重证同一head与空suffix，再经唯一storage durability barrier才返回确定成功。这只把headless closure收口为`INSTALLED_HEAD`；不消费auditor证据当写权、不翻file authority、不写asset/audit/activation/mount，且server/bootstrap/SDK/production router caller为零。
56. core 3.29.1仍保持exact schema-V12 identity与全部错误码/五态，只把auditor和phase-1 finalizer共用的只读事实证明收成private neutral verifier。全私有表逻辑行、TEXT/BLOB字节、SQL VM预算，legacy/production reserved receipt逐行闭合，byte-exact比较和SQLite不可用cause-chain都只有一个owner；auditor负责把typed failure映射成诊断状态，writer负责自己的事务/authorizer/写入与fresh reconcile。neutral leaf不认识五态、detached identity或writer capability，不含mutation SQL，也不反向导入两个consumer。
57. core 3.30.0追加schema-V13 `direct_predecessor_phase_bindings_v1`。四张`STRICT`/append-only表分别固定asset-provider、audit、activation、mount的OLD head、直接证据、外部admission/maintenance观察、runtime/receipt/scheduler checkpoint与NEW head；四个reverse guard既要求NEW head消费本阶段exact pointer，也在推进瞬间重新核对live runtime/receipt/scheduler（audit还重核audit head），因此已提交但过期的binding不能推进head。相邻阶段还逐项串起source/freeze、admission、maintenance proof、receipt root、runtime和scheduler，activation→mount另串after-runtime/install/final admission；首head后generic receipt只许INSERT，所有required timestamp都用NULL-safe canonical校验。V13只接受authentic且17+32数据面全空的V12，最终current表数为17+36；checksum=`c65b56a6…08860e`、fingerprint=`28ea7b26…b25527`、ledger root=`00eacfae…5a23a`。纯合同使用四个独立domain、累积typed head pointer和逐字段重验；`sqlite_cutover_phase_binding_reader.py` 要求调用方持有事务，逐列读取四张表，重建每阶段直接前驱、嵌套 audit→activation→mount 证据、canonical manifest 与 OLD/NEW head，并拒绝缺行、多行、非规范摘要或跨阶段拼接。在3.30.0边界，四个阶段writer与production coordinator均未完成；3.31.0只补上第一段private asset-provider writer。

generic kernel receipt 只证明“这个 SQLite transaction 已提交”，不能单独映射成业务 `CommitReceipt`。3.30.0 已把 source freeze、stage/baseline projection、chronology、incremental head、V12 install carrier、private install写入闭包、phase-1 finalizer、共享neutral verifier和V13四类direct-predecessor binding冻结；3.31.0只新增可执行但仍未挂载生产的asset-provider writer。`sqlite_cutover_phase_binding_reader.py` 会逐字段重建四个binding manifest与canonical head链，global cutover auditor与production authority reader都消费这条同一语义读链，而不是各自做结构 JOIN。但这仍只是未来production coordinator的前驱，不是coordinator本身；audit、activation、mount三个后续阶段writer、coordinator、router和真实caller仍不存在，生产继续消费`FileCommitStore`。只有完成phase writer、durable cutover coordinator、atomic coordinator与consumer接线、完整worker transition/dispatcher、authority router、Context/admission/outbox/subscription同revision接线、外部maintenance lease、长期authenticated checkpoint与百万行性能门，才能宣称生产业务ACID。

当前实现依赖 POSIX `flock`、目录 `fsync` 与 owner/mode 校验；Windows 路径探针只用于 fail-closed 识别，尚未构成可用承诺。Windows 要在后续补齐等价 ACL、进程锁与目录持久化证明后才能进入支持矩阵。

## 2. 不允许协商的安全基线

| 约束 | 固定值或行为 | 为什么不能做成开关 |
|---|---|---|
| 存储位置 | 同一台机器的本地文件系统 | WAL 依赖共享内存，网络文件系统不在 SQLite WAL 支持模型内 |
| SQLite 版本 | `>= 3.37.0` | v2 业务快照表、v3 import ledger 与 v4 catalog ledger 都使用 `STRICT`；旧引擎必须明确 fail closed，不能静默降级成宽松表 |
| journal | `journal_mode=WAL` | 让同机多个 reader 与单个 writer 有明确协调模型 |
| durability | `synchronous=FULL` | committed receipt 必须代表可以在崩溃后对账的结果 |
| relational integrity | `foreign_keys=ON` | 当前 kernel schema 和后续正式关系表都不能被无 FK 的连接写出不一致 |
| schema trust | `trusted_schema=OFF` | 不让 schema 内不受信对象扩大应用能力 |
| identity | 固定 `application_id`，schema 版本与 migration checksum 同时验证 | 防止错库、漂移库或被手改的库被当成健康库 |
| 路径 | 路径组件、数据库、`-wal`、`-shm`、`-journal`、`-bootstrap.lock` 不得是 symlink、特殊文件或多链接文件 | 防止越界、换目标和设备文件风险 |
| 权限 | 私有目录与私有数据库文件；不满足时收紧或 fail closed | 数据库会承载用户数据与提交证明 |

每个新连接都必须设置并读回这些值，不能只在首次初始化时检查一次。运行时唯一可调参数是 `ZE_SQLITE_BUSY_TIMEOUT_MS`，默认 `5000`；它必须是实现声明范围内的正整数，非法环境值 fail closed。它只改变等待另一个本地 writer 的时间，不改变任何安全语义。WAL、FULL 或 local-only 不能被环境变量降级。

## 3. 连接与迁移纪律

- 一个连接只属于创建它的进程；fork 后继承的连接必须拒绝使用。连接也不能被随意跨线程复用。
- 只有显式 `initialize()` 可以创建目录或数据库；`commit`、`inspect_profile` 与 fresh `reconcile` 必须以 no-create 模式打开。数据库或数据卷缺失时，它们返回失败/indeterminate，不能在底层挂载点偷偷造一个空 authority。
- 首次打开与升级都在显式迁移事务中完成。因为 WAL 模式切换发生在事务外，初始化先持有数据库同目录的私有 `-bootstrap.lock`；它只串行 bootstrap，按同一个 busy timeout 有界等待，崩溃时由操作系统释放。拿到锁后仍要在 `BEGIN EXCLUSIVE` 内重读 application/schema/migration 状态。同一路径被多个进程同时首次打开时，迁移最多执行一次，其他进程只能观察完整旧版本或完整新版本。
- migration 有稳定编号、规范 SQL 和 checksum。已经发布的 v1 使用冻结的 legacy whitespace checksum，v2–v6 DDL/checksum 都逐版冻结；特别是 v6 的 `41fe…` 不得被三 revision 修复原地改写。后续版本只能追加新事实，不能借升级改写旧 schema。数据库里记录的编号、checksum、`user_version` 或完整 `sqlite_schema` fingerprint 只要和代码 registry 不一致，就进入 recovery-required；不能用 `IF NOT EXISTS` 悄悄把漂移 schema 修成“差不多能跑”。v2→v3 还必须在任何新 DDL 前确认三张业务表为空、旧 import namespace 未被占用；v3→v4 使用旧 generic identity 正则无法表达的私有 catalog prefix/kind；v5/v6 又分别冻结 repository proof 与 transaction namespace；v6→v7 先做 local revision compatibility preflight，任一失败整笔回滚，不得留下 column/index/临时 preflight 表或推进版本号。
- 调用方只拿到无字段 opaque unit-of-work handle：真实 connection 只在 closure identity ledger 中绑定到 callback 返回，模块不暴露 connection getter/map；executor 在 closure 内完成执行与物化，只返回 `StatementResult`，随后立即 revoke。fork child 直接替换继承的 lock/map。PRAGMA、ATTACH、事务控制、schema 改动和 kernel 私有表读写均被拒绝；terminal marker 建立后 generic commit 在 writer lock 内、callback 前整体关闭。写 receipt 前还会确认事务仍存在，并在同一事务内重验 schema、迁移账本与 authority marker。同进程能用 `inspect` 穿透任意 Python closure 的恶意代码仍不是语言级沙箱承诺，正式不可信扩展须进程隔离。
- 所有会返回 committed receipt 的 writer 统一经过 `_finalize_committed_storage`：先重验数据库、WAL/SHM/journal sidecar 都是 owner-controlled 单链接普通文件，再 fsync 数据库父目录。正常 commit、COMMIT acknowledgement-loss recovery 与 private importer 不得各写一套屏障；receipt 已可见但屏障失败仍是 indeterminate/UNKNOWN，不能报告成功。
- 应用进程不能绕过 SQLite 自己去打开、读取、覆盖数据库或活动中的 `-wal` / `-shm` 文件。备份使用 SQLite backup API，并在恢复演练后再被信任。

## 4. Receipt 与 reconcile 的含义

一次 kernel receipt 只有三个事实：稳定 `transaction_id`、描述用途的 `kind`、数据库产生的 `committed_at`。这里的 `transaction_id` 是数据库事务证明，不是 ProjectTopology 的业务 `intent_id`。

```text
BEGIN IMMEDIATE
  caller writes its SQLite unit of work
  write transaction_id + kind + committed_at receipt
COMMIT
```

结果规则：

- `transaction_id` 唯一，不能产生两份 receipt；
- 通用 `commit` 在打开数据库前拒绝 import 与 catalog 的 reserved transaction prefix/kind；唯一窄权限 importer/sealer 才能在各自单笔事务写 business/import 或 catalog rows 与 exact receipt，普通 UoW 不能抢占身份；
- EMPTY substrate 上，通用 UnitOfWork 对十一张 kernel-private 表的 READ/INSERT/UPDATE/DELETE 全部拒绝，即使 callback 吞掉 SQLite denial 也会 poison 整笔事务、不能 mint receipt；任一 terminal/catalog marker 建立后 generic commit 在 callback 前固定只读。通用 `reconcile` 对 reserved namespace 固定返回 indeterminate；只有 importer/catalog-specific reconcile 可签发正向 exact 结果；
- reconcile 必须打开 fresh connection，只返回数据库里实际存在的 receipt；
- COMMIT 已成立但 ack 丢失时，当前进程仍须在 fresh receipt/exact rows 成立后通过统一 storage durability barrier 才可恢复为成功；若进程立即退出，新进程可按 transaction id 对账到同一个 committed receipt，并在使用前重新执行该屏障；
- `KeyboardInterrupt` / `SystemExit` 等 process-control `BaseException` 不是可吞掉的 acknowledgement loss：kernel 必须先用 fresh reconcile 完成 committed/not-committed/indeterminate 分类（正向分类包含 durability barrier），随后在三种结果下都原样重抛；只有普通 `Exception` 才可按已证明的 committed receipt 恢复成返回值；
- COMMIT 之前退出，新进程只能看到 not-committed，不得依据临时文件、异常文字或调用次数猜成功；
- 不完整、checksum 漂移或无法证明的状态返回 repair/unknown，不能自动重放调用方工作。

repository singleton 明确保持 `authority=legacy`。v2 的三张业务表保存 canonical snapshot；v3 的 import batch/descriptors 将显式选择与 exact receipt 绑定；v4 的 catalog seal/members 再绑定完整 live selection；v5 追加 live/deleted repository proof；v6 追加 transaction schema，v7 只修复 local revision 关系，普通安装后 17 表仍必须为空。private importer/sealer 严格兑现历史列；generic kernel 打开只验证 marker、覆盖关系、长度/计数/外键等结构闭合，不读取全部大 BLOB；shared exact layer 才逐字段、逐字节比较 seal/member/receipt。business commit 不能从 process-local plan 或 generic kernel receipt 猜，只能由 private exact writer 原子写入、fresh reconcile并通过耐久屏障；该 writer 当前没有 production caller。

`GATE-DB-02` 以独立进程、进程间同步点和 `os._exit` 运行这两条崩溃路径，不用 `sleep` 猜时序，也不继承父进程连接。

## 5. Deterministic snapshot codec（W06.2b）

codec id/version 固定为 `ze.deterministic-json` / `1`。这个版本只定义严格 JSON 值图的字节表示：`None`、精确 bool、绝对值十进制数字最多 4300 位的正负 int（符号不计）、有限 float、可严格 UTF-8 编码的 string、list 与字符串 key 的 dict。4300 位边界在 encode/decode 都必须接受，4301 位都必须以 `business_codec_integer_too_large` 拒绝；这是 codec 自有的 CPU DoS 上限，不依赖 CPython 进程级 digit limit，从而保持 3.9/3.13 接受域相同。它还拒绝隐式类型转换、循环值图和超过 128 层的容器嵌套，并在输出前复制已验证值，不修改调用方对象。

业务入口使用 bounded encode/decode：在分配 canonical bytes 或调用 `json.loads` 前，先增量证明 byte ceiling 与节点 ceiling（每个 value、每个 object key 各计一节点）。snapshot v1 每个 envelope 的节点上限固定 250,000；Unicode/control/non-BMP 按实际 JSON escaping + UTF-8 字节计算，NaN/±Infinity 等 parser 常量也在 decode 预检中计数，不能用“最后反正会拒绝”绕过资源门。

encoder 用紧凑格式、排序 object key 和严格 UTF-8 生成唯一 bytes；同一精确类型和值图只有一份表示，`1`/`1.0` 与 `0.0`/`-0.0` 不会被错误合并。decoder 除了做 JSON/类型/重复 key 验证，还会将解析结果重新编码并要求与原 bytes 完全相等。所以“能被普通 JSON parser 读懂”不等于合法；BOM、多余空白、不同 key 顺序、重复 key、非唯一字符转义与数字拼写都会 fail closed。任意已接受值均满足 `decode(encode(value)) == value`，任意已接受 payload 均满足 `encode(decode(payload)) == payload`。

SHA-256 原语只对传入 bytes 计算 32-byte digest，不会同时验证 canonical 形态，也不会和数据库中的预期 hash 比对。canonical 与 digest 必须分别证明；snapshot verifier 在 byte ceiling 后先重算并比对 hash，再进入 bounded canonical decode/node scan。将任意 bytes 交给 hash 函数不能把它升格为合法快照。

异常 message 不回显 payload，底层 parser/Unicode 异常也不保留原输入；但结构化 error path 可能含 object key，跨进程、日志与遥测投影必须脱敏。codec 不替 envelope 决定总 payload/string 大小；W06.2e scanner/prepare 已在 format 天花板内增加批次、行字节和 manifest 配额，SQLite importer 复用同一 business verifier 与 integrity 单一真相源，不得回退到无界入口。

codec 模块不 import/构造 Document 或 Entity，不打开 SQLite，不读写业务/ledger 表，不扫描 legacy 文件，不产生 receipt。所以 W06.2b 自身不改 migration ledger；W06.2h 发布时 registry 到 `user_version=4`，当前 registry 已到 v13，但 v2–v13 表仍全部 private。W06.2c 只在上层消费该 codec 生成和验收业务 envelope；codec 自身仍保持不认识业务模型。

## 6. Business snapshot contract（W06.2c）

snapshot contract 的输入是内存中一个已被调用方持锁的 Document，输出是与 adapter 需要显式写入的 v2 snapshot 列同序、但尚未写入任何数据库的 frozen rows。`projects.repository_singleton` 是 kernel-owned 的常量/default FK，不在业务 row/envelope 内；adapter 必须用命名列并让数据库 default 为 1（或显式写 1），禁止 positional `INSERT ... VALUES`。它分三层建立信任：

1. 每个 Document/Entity row 都对 canonical payload bytes 保存 32-byte SHA-256，envelope identity/state 与行列值必须精确一致；
2. Entity rows 按 `(entity_type, entity_id)` 唯一规范排序，同时 payload 保存每 type 的原插入位次；descriptor 以 `ze.business-snapshot.entity-set.v1` 为 hash domain，按 uint64-BE length framing 流式写入 SHA-256，project root 保存数量与摘要；
3. Project row 还绑定 project/generation 与 Document descriptor，因此缺叶子、多叶子或重编某叶子后的 hash 都会让根验证失败。

Document payload 只收录业务元数据、data schema、config、ACL 和时间，Entity payload 只收录业务 identity/state、provenance、actor log 和时间。Document collections 不重复塞进 Document payload；validator、read-only 标记/source 与 harvest rule 不是 canonical persisted business state。软删 Entity 仍在快照内，hard-remove 的 Entity 已不在当前图；read-only overlay 不入快照且必须以 identity diagnostics 显式报告。

format v1 在业务值域之外还有固定资源上限：project 64 KiB、Document 16 MiB、单 Entity 8 MiB、Entity 数 1,000,000、全部 payload 总和 1 GiB。它们分阶段在相应解码/大循环前 fail closed：先 project+Document，根 descriptor/count 自洽后才预检全部 Entity carrier 并完成聚合 bytes 上限。这些不是建议的日常项目大小；部署配额只能在上层更严。SQLite 版本列只接受 `1..2**63-1` 的 exact int/non-bool，`deleted` 列只接受 exact `0/1` 并与 envelope bool 一致；非有限/负时间、重复 ACL collaborator、非递增 actor-log version 或 provenance/actor 多键少键都会失败。actor-log `fields` 是有序历史，不得被当成 set 规范化。

codec 接受 128 容器层是为 snapshot 包装层留余量，不代表业务对象能消费 121–128 层的值。verify 在 restore 构造前用与 Entity/Document 相同的无 hook clone 重证 120 层/250,000 展开节点值域：Document schema/config/ACL、Entity data、整个 actor log 各按自己的所有权值预算，每个 AiProvenance 则另按 prompt_args 与十字段 envelope 的实际构造语义验收。这些预算不伪装成跨字段共享；整行 codec envelope 的 250,000 serialized-node 限制仍独立且组合更严。

`verify_business_snapshot` 不信任 dataclass 的构造者。它先预检 project/Document carrier 和 bytes，两行各按“codec/format → hash → bounded canonical decode（node scanner 在 `json.loads` 前）→ 闭集/业务值域 → payload—列绑定”验收；根中的 Document descriptor/Entity count 一致后，才预检全部 Entity carrier 和聚合 bytes，再逐行执行同一验收、identity 顺序/每 type 连续位次与最终 project root；它不默默排序不受信任的输入。root 检测不一致和部分改写，不是带密钥真实性；能一致重写全树的攻击者仍需后续 payload-bound receipt 的外部 trust anchor/签名。`restore_business_snapshot` 必须先跑验收，再按保存位次直接构造 Document/Entity、只重装可证明的 framework validator，最后重新 capture 逐行比对；它不调用 `Document.open`、`Entity.from_dict` 或 `Document.add`，也不借恢复重验历史数据。返回值固定 `requires_runtime_rehydration=True`，harvest/read-only/runtime ports 未由 W06.2d 唯一 seam 重装前不得发布、缓存或服务。

该模块是 importer/adapter 内部合同，不进 `sdk.__all__`。它不 import SQLite/kernel、file store、project topology、sdk、Operation/OpLog，不扫描目录、不读写文件、不打开数据库，也不产生任何 receipt。普通初始化的 `kernel_repository.authority` 仍为 `legacy`；当前 `user_version=13`，V2–V9 business/import/catalog/proof/transaction/production/sidecar表均为private，V10只追加guard，V11只重建空mount/head约束，V12追加4张install-closure表，V13再追加4张phase-binding表；generic kernel只接受历史账本闭合且transaction namespace为空的legacy profile。

## 7. Runtime load coordinator（W06.2d）

W06.2d 把“已验证 persisted business head”与“可以交给 Context 的运行态 Document”切成一条同步状态机。restore carrier 的真实签发记录和原 business root 不放在对象 seal 上，而留在 snapshot 模块的 identity ledger；交接时重新 capture、比对并一次性消费。runtime 模块同样只把 declaration/detached/ready carrier 当 handle，project/generation/root/version/session/Document/loader 强引用等权威事实全部留在 module ledger。

生产调用方必须在外层持 topology generation lease、per-project singleflight，并独占未发布 Document。coordinator 在同一线程内再持 exclusive mutation session，顺序固定为：root 重验 → 当前声明安装 → 无 overlay 的 canonical migration → changed/new 行最终 schema 校验 → canonical 有变化时只推进一次 Document version → authority CAS（或 canonical no-op）→ **commit 之后**物化 read-only overlay → business root/runtime inventory 复核 → fresh authority fence → one-shot admission。这样 migration 不会读取外部即时 overlay 来生成不可复现的 canonical 结果，外部也拿不到 pending candidate 后忘记收尾。

注入的 authority writer 接收 `exact_business_snapshot_cas_v1` request：必须以 project/generation/before root/before document version 做 CAS，并精确写入冻结 after snapshot。它同步返回三值 receipt：

- `not_durable`：唯一允许 rollback 的结果；
- `committed`：从这一刻起永久禁止 rollback，overlay/ready 后续失败只能 quarantine 并丢弃该未发布 Document；
- `unknown`：同样 quarantine，fresh authority read/reconcile 后再裁决，不能猜测重试。

Generation lease 不阻止另一个进程推进同 generation 的业务 head；changed CAS 也只证明 writer 当时的比较；canonical root 还不包含 collaboration units/harvest/read-only manifest。因此所有路径在 overlay 完成后都必须调用 `exact_business_head_fence_v1` fresh read，精确核对 project/generation/root/version/declaration digest/runtime generation 并取得 authority revision。Coordinator 不暴露可延迟发布的 ready handle，而在同一次同步调用内完成 fence、session close并直接返回 `RuntimeAdmission`，把 Document 与完整 declaration manifest/collaboration units、root/version/revision 一起交给未来 Context builder，不能只传 Document 再旁路重载 mode。

这仍只是 adapter 可消费的内存协议。W06.2h 已能在严格屏障内把完整 live business catalog 写成历史封印，但它没有耐久 repository revision、屏障释放后的 production source-current fence，也没有改变 repository authority；RuntimeAdmission 尚未由 Context builder 原子 claim。`authority_revision` 尚未接 outbox/订阅 catch-up，quarantine 也没有耐久 repair owner/metrics。声明 digest 中 callable 的 module/qualname 只做本进程诊断，绝不是稳定代码指纹或 exactly-once migration id。Migration/loader 仍是可信 Python callback；exclusive session 会拒绝它们直接 `Document.save`，其他外部副作用仍须由 TCB/进程隔离控制，并由后续 manifest 加行数/bytes/耗时预算。

## 8. Strict legacy scan 与载荷准备（W06.2e）

这一步把“以后 importer 应该写什么”变成一份可验证、但尚未落库的输入。扫描对象必须由调用方逐个给出 project id 和 expected generation；scanner 只接受 ProjectTopology 当前 live namespace，不会遍历 repository，也不会读取 staging/trash/松散旧根。因此 prepared manifest 的 `selection=explicit_projects` 是刻意的：它能证明列出的项目，不能证明 catalog 中没有遗漏项目。每个 handle 只钉住自己的受锁时点；批 prepare 不建立 repository-wide snapshot lease，也不证明多个项目来自同一全局瞬间。

扫描时，外层 topology context 以 repository SH → project lifecycle SH → pinned root fd 固定 generation、live-root inode 和 source head；内层 Document context 再验证 exact capability/root identity并持 content SH。exact Document 读取、business snapshot capture、verify、outer/inner head 对比和 read-only exclusion 检查都必须发生在同一个嵌套 context 内。退出后签发的 handle 只有 identity/version/count/bytes/digests，不携带 `Document`、snapshot rows、路径 authority 或 payload，所以后续代码不能在锁外沿路径“补齐”另一份时点。

多个 handle 只在显式批次中准备。项目先按 project id 规范排序，再用共享 `uint64-BE length || bytes` framing 流式绑定 payload domain、project count、每个 project/document canonical row、每项目 Entity count 和全部 Entity canonical rows；不会再物化一份最多 1 GiB 的拼接 payload。canonical manifest 同时绑定 import id、显式选择、source head、business root、counts/bytes 与 payload digest；manifest、payload、batch 各有独立 domain digest。上限固定为 10,000 个项目、1,000,000 个 Entity、1 GiB row bytes，以及 64 MiB/250,000 nodes manifest。

`FrozenLegacyImport` 和状态为 `prepared_verified_payload` 的 receipt 必须由同一 process-local identity ledger 成对签发。record/reservation 同时绑定 issuer PID + epoch；fork child 直接替换继承锁与 ledger，不能经受支持 seam 使用 parent handle/package/receipt 或 parent reservation token，parent 副本仍有效。这里的 “prepared” 只表示内存载荷已规范生成并重验，不表示：

- 文件在扫描后没有继续变化；
- SQLite 已写入、COMMIT 或 fsync；
- migration 已应用或能跨进程 reconcile；
- repository authority 已切换；
- 整个 catalog 已完整覆盖。

每个 scan handle 有两条独立一次性 lane。payload lane 最多成功加入一个 prepared batch；file-authority runtime compatibility lane 最多把同一 verified snapshot 交给一次 runtime restore/rehydration。两条 lane 可以各消费一次，但互不证明、互不替代；批准备失败必须把整批 reservation 原子退回，不能只吃掉前半批。prepared package 另有一次性的 private adapter reservation seam：`reserve/recover → verify → write → fresh reconcile → verify → consumed/release/quarantine`；active reservation 由强 registry 持有，只有 exact `IMPORTED` 才 consumed，精确 `NOT_IMPORTED` 才 release，其余 quarantine。

reserve 现在一次返回完整 opaque `VerifiedLegacyImportPlan`，而不是裸 snapshots：sealed Project/Document/Entity row graph 会先结构克隆，plan 不与原 record 共享行对象。每个项目持有不可改写的 project/document/entity SQL row views；`snapshot` 只是由这些 rows 现构造的 detached compatibility copy，不能作为 writer 权威输入。plan 原子绑定 plan/import/transaction/receipt intent、manifest/batch/payload protocol、selection、全批 counts/bytes、manifest bytes、三摘要，以及每个连续 ordinal 的 source/storage/generation/Document/business-root/counts/bytes 与 verified row intent。它足以逐字段形成 v3 terminal batch/project 与三张 business rows，importer 禁止再从 package properties 或 `reservation.record` 拼装；但 `committed_at`、source/catalog fresh proof、target outcome 与 authority 均不在 plan 内。plan 无 `__dict__`，不能直接构造/复制/序列化，也不公开；ledger 将它和 exact active reservation 一一绑定，importer 写前/对账后都 recheck 该 pair 与 sealed prepared projection。reserve 保持单赢家；调用方必须在 reserve 前自持 process-local attempt token，recovery 同时核对 exact package/receipt/token。当前唯一 caller 是 module-private shadow bridge，bootstrap/runtime 没有挂载。process-local prepared issuance ledger/plan 不耐久，进程丢失后只能回到 live authority 重扫；已经提交的 exact terminal batch 则可跨进程对账。

W06.2e 本身没有调用 SQLite，也没有 DDL/migration ledger 变化。W06.2h 当时把整体推进到 `user_version=4`；当前已继续追加冻结的 v5 proof、checksum=`41fe…` 的 v6 transaction substrate、v7 local revision compatibility、历史 V8–V12 与 current V13 bindings。V5 proof、preinstall与普通启动仍保持`kernel_repository.authority=legacy`；显式private install可在自身事务内把这个数据库marker CAS为`sqlite`并建立`read_only` runtime，但FileCommitStore的生产事实源地位、文件admission和production router均未改变。

## 9. Schema v3 ledger substrate 的升级与回退边界（W06.2f）

- v1→v2 仍在原有 migration transaction 内创建三张 STRICT 业务快照表；v2→v3 在下一笔原子 migration 中创建 `transaction_receipts(transaction_id, kind)` 精确绑定索引与两张 STRICT import ledger 表，追加 migration ledger、更新 schema fingerprint，并把当前 `user_version` 推进到 3。旧 repository `created_at` 与所有非保留 transaction receipts 必须原样保留。
- `legacy_import_batches` 只接受终态 `imported`：exact import id 派生 namespaced transaction id，并闭集绑定 receipt protocol/kind/status、manifest/payload/batch protocol、`selection=explicit_projects`、counts/bytes、canonical manifest 与三组 digest；外键要求数据库中存在 exact `(transaction_id, kind)` kernel receipt。
- `legacy_import_projects` 只接受归属于 exact batch 的 canonical descriptor：ordinal、project/generation、Document id/version/storage、source-head digest/bytes、business root、Entity count 与 row bytes；ordinal、project id 与 document id 只在同一 batch 内唯一。历史行不外键到会继续演进或删除的 current business tables，只外键归属 immutable batch，避免把历史证据错误绑成当前快照的生命周期从属物。
- 两张账本不保存 `prepared_verified_payload`、carrier reservation、consume flag 或其他进程内状态。schema migration 不扫描 legacy 项目目录、不消费 prepared package、不插入业务/ledger 行；五张表为空是成功条件，不是“尚未同步完”的后台状态。
- v2→v3 必须在执行任何 v3 DDL 前拒绝三张业务表中的任意旧行，也必须拒绝 transaction id 以 `legacy-import:` 开头或 kind 为 `legacy_import_v1` 的既有 receipt，固定报 `sqlite_legacy_import_migration_dirty`。这个保留 identity 空间只能由未来受控 importer 在 v3 exact schema 下与正式终态账本同事务使用，不能继承一条没有 payload binding 的旧 receipt。
- 任一前置检查、DDL、checksum 或 fingerprint 失败必须回滚完整 migration：数据库仍是可验证的 v2，`user_version=2`，不存在新索引或半张 ledger 表。不能“先升级再异步清理脏行”。
- `documents.document_id` 按既有 Document 合同全局唯一；历史 duplicate 复制同 id 时，后续 importer 必须先报冲突并显式修复，不能静默合并或放松约束。
- 普通 `UnitOfWork` 被 authorizer 禁止写三张业务表和两张 import ledger 表；W06.2g 的唯一 importer 使用更窄、可审计、固定 INSERT-only 的内部入口。没有 terminal marker 的 v3 若出现任一业务/ledger/reserved receipt 行，固定以 `sqlite_import_state_unexpected` fail closed，不能把越权行默认为合法历史。
- 旧 kernel 看到 v3 会因 schema version 超前而 fail closed。若需要回退，必须在升级前用 SQLite backup API 留备份并恢复该备份；禁止修改 `user_version`、删除索引/表或清空账本来伪造降级。

## 10. Schema v4 historical catalog seal（W06.2h）

v4 在冻结的 v3 import substrate 之后只追加两张 `STRICT` 历史表：singleton `legacy_catalog_seals` 与按 ordinal 归属的 `legacy_catalog_members`。catalog transaction 使用 `_legacy-catalog-seal:` / `_legacy_catalog_seal_v1` 私有身份；这组值刻意不满足 v1–v3 generic transaction identity 正则，因此旧库中合法存在的相似普通 receipt 不会阻塞升级。seal 固定绑定 canonical manifest、source-catalog/target-coverage/seal 三组 domain-separated digest、v4 schema fingerprint 和 `cutover_ready=0`。非空 seal 必须精确指向唯一 terminal import 及其全部 descriptors；empty seal 必须 `import_id=NULL`、零业务行、零 import batch、零 member。

外围严格协调器按 barrier EX → repository EX 持锁，完整 context 覆盖 strict enumeration、两轮 scan、terminal import、catalog seal 与 final fresh reconcile。所有受支持的普通 repository/project lifecycle 与 activity lease 都先取得 barrier SH，所以它们不能在证明窗口中推进 catalog 或业务 head。严格枚举会拒绝未决 recovery/journal、非空 staging/trash、非终态/错路径/多余 state、缺失或孤立 root、不安全 inode、marker/generation/Document identity 不一致；空目录不是遗漏，而是可封印的显式状态。fork child 只关闭继承的锁 fd，不向共享 open-file-description 发 `LOCK_UN`，避免误解锁父进程。

非空目录第一轮 scan 生成 content-derived stable import id 并落 terminal shadow；第二轮必须重新 scan、重新 prepare、保持另一条 active plan binding，且 descriptors/import id 与第一轮完全相同，才允许 seal。空目录不创建 scan/prepare/import lane。sealer 只有 marker、receipt、seal、member 四类固定 plain INSERT，authorizer 拒绝 UPDATE/DELETE/DDL/PRAGMA/ATTACH/事务逃逸；生产方法没有 callback/connection 参数。shared exact 层是 importer 与 sealer 的唯一 catalog 正向定义：seal/member/receipt 任一字段或等长 BLOB 被改写，两边都必须拒绝，不能签发矛盾结果。

这份证据的准确读法是：“在 barrier 仍持有的完成时点，SQLite 精确覆盖了当时全部 live business project。”它不是持续 current 的租约，不覆盖 deleted tombstone、generation high-water、OpLog、assets、inbox/outbox、pending confirmation、action/pipeline job，也不授予 runtime 读取。释放 barrier 后 legacy authority 可以继续写；结果固定 `authority=legacy`、`historical_fenced=True`、`cutover_ready=False`。

## 11. V5a.1 完整仓库精确证明（private / unmounted）

v5 的三张 proof 表现在已有一条可执行但未挂载生产的窄写入链。`sqlite_legacy_repository_exact.py` 是唯一正向定义：它只接收 core 重新规范化的 v4 catalog projection 与 typed live/deleted member union，重新派生全部 integrity、schema fingerprint 和 live↔catalog coverage；任意 raw dict、伪 projection、缺项、错 ordinal 或 deleted 残留 live 引用都不能进入 writer。

`sqlite_legacy_repository.py` 只允许四类固定 plain INSERT：proof control、私有 transaction receipt、seal、members。writer 在 `BEGIN IMMEDIATE` 下先重证 v4 exact substrate 与全 v5 namespace 为空，再原子写入；authorizer 拒绝 UPDATE/DELETE/DDL/PRAGMA/ATTACH/事务控制。COMMIT 后无论正常返回还是异常返回边，都重新开连接，在同一 SQLite snapshot 内流式精确比较 v4 catalog/import/business coverage、v5 control/seal/member 和完整 receipt namespace，并重复 storage-file/WAL-sidecar 检查与父目录 fsync。只有 fresh exact `SEALED` 能恢复普通 acknowledgement loss；`KeyboardInterrupt/SystemExit` 在完成 fresh 分类后仍原样传播；BUSY/LOCKED/IOERR/CANTOPEN/FULL/NOMEM/INTERRUPT 与耐久屏障失败只能是 `UNKNOWN`，不能伪报损坏或成功。

SDK 私有协调器在同一 barrier EX → repository EX 中完成整条 `S0 → v4/current exact projection → S1 → v5 seal + fresh reconcile → S2`。namespace `ABSENT` 才调用旧 v4 writer；`PRESENT` 路径只做两轮当前源扫描，在 active reservation 内复制 core projection，并在返回前显式 release 两条 lane，避免旧 v4 的历史 receipt-total 合同把合法 v5 receipt 当成污染。live member 逐项绑定 generation、source hash/bytes 与全部 catalog descriptor，deleted member 绑定 operation 与 generation high-water。返回载体不泄漏路径、handle、plan、projection 或 writer capability。

这份 proof 永久保持 `authority=legacy / authority_state=cutover_prepared / cutover_ready=0`。当前调用还必须由测试显式提供 `runtime_generation + code_digest`；在 Skill.Sync checksum/部署 identity 被抽成共同 leaf 并形成 production issuer 以前，coordinator 明确 `mounted=False / production_callers=0`。它不推进 admission、不授予读写权，也不证明屏障释放后的 currentness。

## 12. 后续怎样切业务 Authority

3.31.0 已完成 durable source freeze、canonical asset stage-member/baseline投影、V11 chronology closure、可增量五阶段head、V12 member-mapping/baseline-outer carrier与fresh read、private install writer、phase-1 finalizer、V13四类direct-predecessor carrier/guard/fresh semantic read，以及第一个asset-provider fixed-SQL writer。可表示性、install写入闭包、plan-free六恢复点重建、headless closure收口、binding全字段读写投影和phase 1→2事务已经解决，但audit/activation/mount三个writer与coordinator仍不存在。普通initialize仍legacy/empty，生产仍只写`FileCommitStore`。以下顺序仍一项都不能省：

1. 把现有 plan-free auditor 从已支持的 CLAIM/AUTHORIZE_EFFECT/COMPLETE/RECONCILE、多 allocation、pipeline control与non-outbox genesis扩展到未来合法的 renew、expire、后续 retry claim及pipeline/action worker execution；在尚未具备精确重建规则时必须继续返回 `UNKNOWN`，不得把“不认识”降格成成功或损坏。V10 的 zero-control历史commit只能证明其scheduler引用是一个真实完整endpoint，缺少独立as-of因果标记；长期必须用全局事务序、checkpoint、authenticated skip-link或accumulator补齐唯一时点证明并取代有界线性扫描，同时建立真实数据规模的时间、锁等待与内存门禁。
2. **已完成（3.23.0）**：production writer覆盖 pipeline control、durable action与多 outbox，target/genesis、control receipt、shared ledger、allocation和runtime CAS保持同一 `BEGIN IMMEDIATE`；同 snapshot rooted reader不接原 plan，发布前同时 fencing runtime/worker/dispatch。后续 atomic-v2 扇出与历史版本化 snapshot必须另立协议，不能回改本项。
3. **carrier/reader/install writer/global auditor/phase-1 finalizer与asset-provider writer已完成，后三阶段writer待完成**：V12提供member mapping与baseline outer closure；3.27.0 writer以fixed SQL写install-owned surfaces；3.29.0 finalizer在自己的`BEGIN IMMEDIATE`内重证raw closure/runtime/空suffix并只写initial head；3.30.0为四个后续转换增加direct-predecessor carrier、正反guard与fresh全字段reader；3.31.0再以共享纯mapper和唯一91列INSERT→head CAS写完phase 1→2。下一步实现audit/activation/mount三个fixed-SQL writer，再实现durable cutover coordinator：在同一外部maintenance lease与严格EX租约内安装/重证source freeze，按`coverage ≤ install ≤ audit ≤ activation ≤ mount`逐段写receipt，以canonical projection与增量head做fresh reconcile；writer不能顺手翻file authority，测试fixture不能成为production issuer。
4. 实现 outbox dispatcher，并把它精确接到现有首次 CLAIM 与 AUTHORIZE_EFFECT/COMPLETE/RECONCILE writer；再补 renew、worker RETRY、expire、durable action worker、typed pipeline worker与 repair API。每个 lease/fence/attempt/current pointer从耐久事实恢复；business control、worker execution与外部 effect各用独立 identity，跨 revision claim在 cursor CAS同 snapshot查询直接后继。
5. 新增 atomic coordinator/planner，把 exclusive Document candidate、serializable action、confirmation/inbox与 commit前 canonical event一次组装；Context、admission、outbox与订阅消费同一 authority revision。现有 `FileCommitStore`接口不能伪装承载缺失字段。
6. durable source freeze本身已经停止普通legacy写入；coordinator仍需在repository barrier内重证marker/source currentness/target head并完成outbox catch-up，才把authority router切到SQLite。freeze之后没有“删除marker重新写legacy”的回退，只能pre-freeze用backup回退或post-freeze forward-repair，禁止长期dual-write。
7. 以多进程 head CAS、证书/runtime同事务CAS、事件顺序、确认恢复、action补跑、资产故障、备份恢复和旧数据升级门禁证明成熟度；对百万级 full snapshot/manifest/closure audit建立最大 RSS、P95/P99 lock wait及 transaction/read/reconcile时延硬门。全部达标前不得切 authority或删除 bridge。

跨主机、多设备离线和冲突合并仍是更上层的同步协议问题。SQLite/WAL 只承诺同机本地文件系统上的事务事实，不自动变成分布式数据库。

## 13. 可审计证据

- `GATE-DB-01`：真实打开数据库，逐连接读回固定 PRAGMA，验证 application/schema/migration checksum 与已知 `sqlite_schema` 对象集合、local-filesystem 判定、路径类型、busy timeout 范围和权限 fail-closed。
- `GATE-DB-02`：独立进程在 COMMIT 前后真实退出；新进程 fresh reconcile，验证 committed receipt 可找回、uncommitted 不冒充，同一 transaction id 不产生重复 receipt。
- `GATE-DB-03`：真实构造 V1 库并逐版升级到 current V13，验证旧 receipt/authority 保留、冻结的 V2 三张 STRICT 表和关系约束精确成立、全部后续私有表为空且普通 UoW 无权写。
- `GATE-DB-04`：真实执行 codec 固定向量、顺序无关编码、双向逆变换、正负 4300 位 int 接受/4301 位拒绝、非法/非 canonical 拒绝、深度/循环边界与精确 SHA-256；同时审计 codec 未依赖 Document、Entity 或 SQLite。
- `GATE-DB-05`：真实 capture/verify/restore 一个含多 Entity 的 Document，证明缺/多/重编叶子均被 project root 发现，恢复不经 legacy 打开/构造/添加入口；静态审计 snapshot 模块不依赖 SQLite/file/topology/sdk/OpLog，并在 fresh isolated current-V13 database 真实读回 `authority=legacy` 与全部 private 表初始为空。
- `GATE-DB-06`：真实执行 rooted restore handoff、exact-one version/root CAS、commit 后 overlay、changed-row schema 拒绝、rollback 与 one-shot admission；静态审计 state/projection/coordinator 三层依赖单向、公开对象原样 re-export、runtime seam 不依赖 storage/topology/log/cache/I/O 实现且不公开 pending candidate/proof/unverified legacy issuer。
- `GATE-DB-07`：真实扫描 v2/raw-legacy live project，证明 outer topology generation/root 与 inner Document content lock 覆盖同一 capture+verify 时点、outer/inner head exact；执行 canonical manifest/virtual framing、package-receipt pairing、完整 opaque plan 逐字段对账、row graph 隔离、exact pair binding/recovery、整批失败零消费、payload/runtime 独立 one-shot、显式 adapter 三态，以及在另一线程持旧锁时真实 fork child 无法复验继承 reservation、parent release 后可重试和 runtime admission。静态审计 plan 未进入稳定公开面、state ← facade 单向依赖、唯一私有 scan seam 消费者、`os` 能力仅限 PID/at-fork、无原始路径/SQLite/kernel/OpLog/cache/I/O 旁路，并把 private plan consumer 限定为唯一 SQLite bridge。
- `GATE-DB-08`：以冻结的 `MIGRATIONS[:3]` 真实执行 fresh v3 与 v2→v3 migration，验证 transaction receipt exact binding index、两张 STRICT import ledger 的 FK/CHECK/唯一性和闭集；同时验证 current fresh/upgrade 会继续到 v13，v3 历史 DDL/checksum 不因后续 migration 漂移。脏 v2 与 reserved import namespace 仍须在任何 v3 DDL 前整笔回滚；authority 仍为 legacy、生产 store 仍为 FileCommitStore。
- `GATE-DB-09`：真实执行 EMPTY→TERMINAL_SHADOW 首导、exact retry、same-id/cross-id conflict、disjoint batch、row tamper、authorizer rollback、database/finish acknowledgement loss、generic COMMIT 后 async cancellation fresh 分类再重抛、durability-barrier failure→UNKNOWN/quarantine，以及 import 已返回但 fresh reconcile 尚未开始时的 async interrupt→quarantine；静态钉住共享 integrity 算法、tuple-subclass 底层槽位冻结、固定 INSERT-only SQL、唯一 core writer/private SDK bridge、共用 kernel durability barrier、无公开/bootstrap/runtime caller，并重证 authority 始终为 legacy。
- `GATE-PROJECT-09`：真实与静态审计 catalog barrier 的 SH/EX 准入、barrier→repository→project→Document 锁序、严格 full-catalog 枚举的 fail-closed 矩阵，以及 fork child 关闭继承 fd 但不 `LOCK_UN`；确认能力不进入公开 SDK surface。
- `GATE-DB-10`：真实执行 fresh/v3→v4、empty/nonempty historical seal、两轮全目录扫描、exact retry、namespace collision、固定四 INSERT/authorizer、ack-loss/async、seal/member 等长篡改与 importer/sealer 一致分类；静态限定唯一 private coordinator/capability caller，并重证 `authority=legacy`、`cutover_ready=0` 与 tombstone/OpLog/assets/outbox/action 不在证明范围。
- `GATE-PROJECT-10`：崩溃并恢复 admission 安装的三个耐久边，证明唯一 production rollout caller、全普通入口 pre-I/O fence 和 live+deleted 完整快照。
- `GATE-DB-11`：冻结 v1–v5 migration/checksum，执行纯 repository integrity 与 deleted high-water proof，验证三张 STRICT/private/append-only 表和持久 `authority=legacy / authority_state=cutover_prepared / cutover_ready=0`。
- `GATE-DB-12`：执行 empty/deleted-only/live/mixed exact proof、幂等与双进程收敛、固定四 INSERT/authorizer、部分 namespace/receipt collision、同 id 等长篡改、ack-loss/async/durability 分类，并静态证明唯一 private writer/SDK bridge 与 production caller=0。
- `GATE-PROJECT-11`：证明同一 barrier 覆盖 S0→v4/current projection→S1→v5 fresh reconcile→S2；PRESENT 路径不得调用旧 v4 writer，所有 active plan lane 必须显式释放，且协调器不得执行 admission CAS。
- `GATE-DOC-10`：证明 opaque Document candidate 只由 exact exclusive session 签发一次，绑定 PID/thread/head，未提交可完整回滚，关闭后永久失效。
- `GATE-COMMIT-08`：真实构造并重验完整 `AtomicCommitPlan`，锁住 source admission、snapshot、Operation/target/outbox、action/pipeline、asset、inbox 与 confirmation resolution 的计数和摘要闭包。
- `GATE-DB-13`：冻结 V1–V6 migration/checksum（V6=`41fe…`），验证 17 张 V6 `STRICT` 私有表在 current V13 fresh/upgrade 后全空、reserved namespace 不可被 generic UoW 使用，且 authority 仍为 legacy。
- `GATE-PROJECT-12`：这是一条冻结的历史 V12 证据，静态与运行时证明 V6 plan/candidate 没有 production caller/export，且当时普通 V12 启动不挂载、不翻权、17 张 V6 transaction 与 32 张 production/runtime 表全空；current V13 的 17+36 空启动边界由后续门继续证明。
- `GATE-COMMIT-09`：runtime 执行 pre/post-COMMIT、ack-loss、process-control 与 durability fault matrix；AST 强制真实 child `os._exit` before/after 两态测试仍可执行，并冻结 change-v2/baseline-v1 独立 domain。
- `GATE-COMMIT-10`：在 Worker 投影、首次 OUTBOX CLAIM 与 schema-v9 `AUTHORIZE_EFFECT / COMPLETE / RECONCILE` fixed-SQL 垂直之上，继续冻结通用 pipeline/action/multi-outbox production writer、transition CAS、reserved control identity preflight、同 snapshot plan-free rooted reader、多 allocation/pipeline-control durable audit和合法 Worker 后缀重联；聚焦回归覆盖事务故障、ack-loss、篡改、I/O、资源预算、五态分类与第二轮 runtime/worker/dispatch publish fence。不支持的 renew/expire/retry、ACTION/PIPELINE worker execution和 atomic-v2扇出必须 UNKNOWN；本门仍不冒充 dispatcher、authority router或生产挂载。
- `GATE-COMMIT-12`：静态锁定private V12 install writer的API、16条exact plain INSERT列/值位次、唯一marker CAS、完整authorizer、helper外无mutation、V3 terminal-shadow三表只读、legacy/production receipt CAST-aware闭包、唯一coverage/stage结构、closure-last、同snapshot恰好两次fresh语义读回、COMMIT后新连接reconcile、V12 identity与server/bootstrap/sdk/production caller为零。独立runtime probe覆盖empty install/rollback/retry/ack-loss/durability、真实BUSY、同/异projection双线程竞争、production/legacy BLOB污染、rollback/commit-close uncertainty、stable kernel CORRUPT与process-control原信号；live/mixed/deleted、asset coverage metadata、partial namespace、额外V3行和active suffix由focused writer suite另证。本门不声称已有真实多进程竞争或等长语义篡改证据，也不冒充cutover auditor/coordinator/router。
- `GATE-COMMIT-13`：静态锁定private global cutover auditor的database-only API、SELECT-only双snapshot、六恢复点、五态、资源预算、detached非权限、V12 identity与production caller为零。独立probe真实执行coverage-only/stage-only/mixed preinstall、首张INSTALL因果receipt root、合法/任意root periodic、真实business+worker+periodic suffix、结构异常双观测及cleanup、TEXT/BLOB预算、篡改/fence/I/O/process-control；successor receipt root与worker as-of在没有authenticated checkpoint时必须UNKNOWN，不能借时间戳猜测。
- `GATE-DB-14`：冻结 v1–v6+v7，真实执行 authentic v6→v7 和合法 A1→B1→A2，得到 global/project/head=`3/2/2` 并 fresh reconcile。
- `GATE-DB-15`：真实读取 snapshot/Operation/outbox/action/pipeline/asset/inbox/confirmation 全闭包，验证重叠 commit 按第一个 `BEGIN` 线性化。
- `GATE-PROJECT-13`：全树反向证明 baseline/cutover/asset/audit/activation/mount/phase-binding/atomic-control/worker/projection/canonical-root contract、历史 V7/V8/V9/V10/V11 runtime/writer/reader，以及 current-V13 production authority/writer/rooted reader、install-closure semantic reader、phase-binding mapper/reader、private V12 install writer、asset-provider writer、first-claim writer、payload projection writer、durable auditor与专用test-support只存在精确依赖、call/reference白名单，没有server/bootstrap/public SDK caller；migration root只能由`sqlite_schema`调用一次，receipt root只能由`sqlite_business_audit`调用一次。asset-provider writer已经存在但仍private/unmounted且production issuer/caller为零；本门不证明audit/activation/mount三个writer或coordinator/router。普通schema-V13启动仍legacy，17张V6 transaction与36张production/runtime表全空。
- `GATE-DB-16`：永久冻结历史 V8 migration checksum/schema fingerprint/migration root，显式在 V8 registry 内真实执行 fresh/pristine-V7 upgrade、25 表结构与17+25空表、脏 V7 整笔回滚、旧 V7 writer fence。
- `GATE-DB-17`：永久冻结历史 V9 三项 identity 与 exact 25+3 表所有权，真实执行 fresh V9、authentic V8→V9、前八条 ledger 保留、脏 V8 preflight 原子回滚和旧 V8 writer/reader callback-zero fence；它仍只证明 sidecar schema、触发器、迁移与版本隔离，writer/auditor 的事务和审计语义由 `GATE-COMMIT-10` 另行证明。
- `GATE-DB-18`：冻结历史 V10 migration checksum/schema fingerprint/migration root，证明 V8/V9 前缀逐字不变、零新表、34 个 timestamp trigger 与 2 个 dispatch tuple trigger全量存在并能拒绝 24-byte `CHECK(NULL)` 垃圾和部分元组；真实执行 fresh V10、authentic V9→V10、脏 V9 preflight 原子回滚及旧 V9 writer/reader callback-zero fence。它只加固历史 schema，不证明 V11、dispatcher/cutover/router 或生产 caller。
- schema-v11/head/freeze focused suites：执行fresh/authentic-empty-V10→V11、脏namespace/非legacy原子拒绝、旧V10 binary fail-closed、反序mount/split revision拒绝；固定五态head vectors与逐pointer后继影响；证明source freeze exact retry、ack-loss/process-death恢复、unsafe carrier拒绝及全部普通legacy入口pre-I/O fence。
- schema-v12/install-closure focused suites：执行 fresh/authentic-empty-V11→V12，冻结 V1–V11 identity，验证只新增 4 表且 `PRODUCTION_RUNTIME_TABLES=32`；覆盖 16 MiB/4 块/64 MiB 边界、稠密 chunk、inner receipt mirror、三组空 target 根、`available` revision 1 + fresh coverage、closure/head 前驱、闭包后追加与等长语义篡改。fresh semantic reader 必须将后一类“关系正确但 hash 错误”数据库拒绝授权；fixture 不能冒充 fixed writer/coordinator/auditor。
- kernel 单元测试：覆盖并发首次迁移、schema/checksum 漂移、symlink/特殊文件、PID/线程连接归属和 Python 3.9 import。

SQLite 行为依据：[WAL 模式与同机限制](https://www.sqlite.org/wal.html)、[PRAGMA 参考](https://www.sqlite.org/pragma.html)、[避免数据库损坏的文件访问纪律](https://www.sqlite.org/howtocorrupt.html)。
