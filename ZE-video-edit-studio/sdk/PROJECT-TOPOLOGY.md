# Project Topology — 项目目录生命周期协议

## 一句话结论

项目不是“一个可以随时 `copytree/rmtree` 的文件夹”，而是带**主体、身份、代际、租约、事务证据和恢复状态**的命名空间。所有创建、复制、重置、删除以及旧版目录迁移，只能经过 `ProjectTopologyCoordinator`；所有可能写项目的数据流，在真正写入前都必须持有该项目当前 generation 的 activity lease。

这层解决的是文件兼容后端上的正确性底座。它已支持同机多进程并发和进程崩溃恢复；W06 的事务数据库会替换状态与 journal 的存储介质，但不会改变调用方看到的 generation、intent、lease 与 reconciliation 合同。

## 为什么单独成层

旧实现把项目生命周期散落在 `Repository.create/duplicate/open_demo/delete`：每个方法各自决定临时目录名、复制范围、锁和清理顺序。单进程顺序执行时看起来可用，但一旦出现两个进程、旧 Context、后台 pipeline 或 rename 后断电，就会产生四类严重问题：

1. 失败方可能删除并发成功方刚发布的目录。
2. 删除/重置后，旧进程仍握着对象并把旧项目重新写回来。
3. “文件已经移动，但响应丢了”被误报成普通失败，重试制造第二次业务操作。
4. 启动恢复无法区分完整项目、半成品、旧备份和应该物理清理的垃圾。

独立层的意义是：业务回调只负责“新项目里面应该有什么”，生命周期层唯一负责“它何时可见、谁可以写、失败后如何证明和恢复”。

## 存储布局与唯一责任

```text
.data/
├── projects/
│   ├── <project_id>/                 # 唯一 live namespace
│   │   ├── .ze-project.json          # project_id + generation + operation_id
│   │   ├── document.json
│   │   ├── entities/
│   │   └── assets/
│   ├── .ze-staging/<operation_id>/   # 未发布的新代
│   └── .ze-trash/<operation_id>...   # 已移出的旧代 / 待 GC 数据
└── .ze-topology/
    ├── repo.lock                     # 整体命名空间顺序
    ├── locks/<sha256(project_id)>.lock
    ├── state/<sha256(project_id)>.json
    ├── journal/<operation_id>.json       # 进行中 / 崩溃恢复证据
    ├── intents/<operation_id>.json       # principal hash + target + kind 的永久认领
    └── outcomes/<operation_id>.json      # 主体绑定的不可变 committed/deleted 回执
```

- topology 的 repository/project lifecycle 锁都在 live 目录外，所以 rename/delete 不会让命名空间锁路径漂移。live 项目内的 `.ze-document.lock` 只负责该目录自身的 Document head/generation 内容读写：open=SH、save=EX；duplicate 不复制源锁 inode，目标在验证时创建自己的锁。
- marker 证明“这个目录是谁、哪一代、由哪次操作发布”。
- state 证明目录的逻辑状态：`live / quiescing / deleted / recovery_required`。
- journal 是进行中与跨崩溃恢复证据，不是普通日志。
- intent claim 在 mutation 前先耐久化，只保存域分离 principal hash，不保存账号或 guest 原文；
  即使操作明确未提交并释放 journal，同一个 operation ID 也只能由原主体、原 target/kind 重用。
- intent status GET 是严格纯读：无 journal/outcome/claim 的随机 intent 只返回“在当前认证主体
  scope 内，此刻没有提交证据”的 `not_committed`，不创建 claim、project lock 或其他控制文件。
  这次观察不是认领；只有真正 mutation POST 才能在 repo 独占锁内首次认领
  principal + target + kind，避免匿名对账耗尽 inode/磁盘。
- outcome 是操作的不可变终态回执，并绑定 `principal_id + kind + source + target`。后续 reset/delete/create 推进 generation 时，旧 intent 的成功证明仍然可查，不依赖“当前 marker 恰好还是它”；另一个账号或 guest 即使拿到相同 intent/target，也不能认领、查询或重放原主体的结果。
- journal 只有在 outcome 完整原子写入并完成目录 fsync 后才可删除。旧代物理清理可以失败并延后，但不能先删 journal、后补终态回执。
- staging、trash 都以 32 位随机 intent ID 命名。pending journal、永久 claim 或 committed outcome 已占用的 operation ID 永远不能覆盖另一 principal/target/kind 的证据；完整回滚且确定无 namespace effect 的 prepared 操作只释放 journal，不释放 claim，同一主体仍可用同一 intent 做唯一一次有界重试。
- projects/control/staging/trash 与 lock 都必须是真目录/普通文件。旧根迁移和 duplicate 共用唯一 `copy_durable_source`：从 pinned source/target root fd 逐层 `scandir + openat(O_NOFOLLOW) + fstat` 流式复制，只接收当前 euid、非 group/world writable 的真实目录与单硬链 regular file；symlink、hardlink、FIFO/device/socket、读取中 inode 变化一律在 staging 发布前失败。复制结构预算固定为深度 64、10 万 entry、8 GiB；错误只给稳定 reason，不回显 entity/asset 子项名。协调外的恶意同 UID 进程仍不在隔离承诺内。

## 两层并发协议

### Repository writer admission（V5a）

`repository-admission.json` 先于下述任何 repository/project/Document 锁与 I/O 验证文件 writer 权限。记录含不变 epoch、单调 revision 和 `legacy / cutover_pending / sqlite`；状态边不可逆地固定为 `legacy R → cutover_pending R+1 → sqlite R+2`，普通入口只接受 `legacy`，pending/sqlite 在 callback 或磁盘读写前统一返回 `project_authority_moved`。进入 pending 后恢复只能向前，不能用回退重新开放旧 writer。未来 SQLite 读写必须先在 barrier SH 下取得 exact lease，绑定 epoch、cutover id 与 source revision 的最终后继，再按 file barrier → SQLite transaction 的唯一锁序工作。首次安装用 `prepared → record → committed` 持久标记恢复崩溃；只有 production `Repository` bootstrap 能在 barrier EX 下将严格闭合的 pre-v5 store 升级，已安装栅栏被删/损坏不会被普通 constructor 复活。

private strict repository snapshot 在 barrier EX → repository EX 内交付排序后的完整 state descriptor：live 绑 Document exact bytes/hash，deleted 绑 tombstone operation 与 generation high-water。这只是后续 SQLite proof/cutover 的受限输入，不进入 `sdk.__all__`，不表示 authority 已切换。

### Same-barrier repository proof（V5a.1，private / unmounted）

私有协调器把这份 strict snapshot 变成一份可重复对账的 v5 历史证明，但从 S0 到 S2 始终不释放 barrier EX → repository EX。它先用 core namespace probe 分路：v5 完全为空时才运行原 v4 terminal import/catalog writer；已有 v5 control 时，旧 v4 顶层校验器会因为新增 receipt 而失去适用性，因此只做两轮当前源扫描，在 active reservation 内重建 core-normalized projection，并在返回前显式 release 两条临时 lane。

随后协调器把每个 live state 的 generation、Document exact hash/bytes 与 v4 catalog descriptor 逐项绑定，把每个 deleted state 的 tombstone operation 与 generation high-water 纳入同一 ordered union；S1 重证文件源未漂移，v5 writer 执行固定四类 INSERT 并 fresh exact reconcile，S2 再重证源仍等于 S0。任何一边失败都不能产生正向结果。返回值不含 path、handle、plan 或 projection，并固定 `authority=legacy / cutover_ready=False / mounted=False / production_callers=0`。

这仍不是 cutover coordinator：它不修改 `repository-admission.json`，没有可信 runtime/code identity 的 production issuer，也没有挂入 Repository、server、CLI 或 runtime。屏障释放后文件 authority 仍可继续写，所以该结果只能读作“这一受锁完成时点的精确历史事实”，不能作为持续 writer fence 或 SQLite runtime admission。

### Repository lock

拓扑读取拿共享锁；create/duplicate/replace/delete/recovery 拿独占锁。它保证项目名空间的检查和切换不会与另一项拓扑操作交错。

### Project lifecycle lock

正常读取和写入拿共享 activity lease；删除或重置在切换目录前拿独占 lease。锁文件位于项目目录之外，因此即使 live 被 rename 到 trash，旧 Context 仍竞争同一把稳定锁。

`ProjectHandle` 在打开时冻结 `generation`。CommitCoordinator、PipelineSupervisor 和 asset 上传都会在工作开始前重新核对：state 必须仍是 live，marker 必须匹配，generation 必须没有变化。浏览器的 command、confirm/cancel、undo/redo、asset、AI、duplicate/delete/reset 还必须显式提交自己实际看过的 `expected_generation`；服务端不能拿“当前代”替客户端补默认值。删除或重置推进 generation 后，旧 Context 固定得到 `project_generation_stale`，不能复活旧目录。

`ProjectHandle` 不是 legacy scanner 的文件 authority：它只冻结路径与 generation，不携带目录 inode，也不持有 Document content lock。Scanner 必须使用私有 `open_live_scan_source(project_id, expected_generation)`；该 context 按 `repository SH → project lifecycle SH → pinned root fd → Document SH` 获取资源，以 `O_NOFOLLOW + O_DIRECTORY` 打开 owner-controlled live root，并在 yield 前后同时重证 state/marker/generation 与 named `(st_dev, st_ino)`。返回的私有 sealed source 只携带 project/generation、`document.json` 路径、root identity 和已验证 head，不暴露 fd，也不进入公共 SDK barrel。Scanner 必须在 context 内完成绑定同一 root identity 的完整 Document 读取与 business snapshot capture；退出后不能拿 source 路径继续扫描。

普通 HTTP/MCP/CLI 读请求也持短 activity lease，避免一个请求横跨两代拼出混合快照。SSE/AI 这类长任务不能永久持共享锁：SSE 每秒做一次瞬时 generation、session、tool policy、durable ACL 与 demo owner 校验；发现换代发 `resync_required`，发现失权发 `access_revoked`，随后关闭旧流。浏览器的 snapshot 同时取得 `event_cursor + event_epoch`，只从 `cursor+1` inclusive replay；generation、epoch、document version 或 seq 任何一轴倒退/跳洞都整份重取。AI 子进程的 MCP URL 固化启动时 generation，AI HTTP 回执也标起始代；任务期间若项目被重置，它的后续工具调用得到 `project_generation_stale`，旧代回复也不会进入新代 UI。这样 demo 原地 reset（ID 不变）也不会继续消费旧 broker/Context。

## Document head 内容快照与缓存漂移

topology generation 只回答“是不是同一个项目目录世代”，不能回答“这个世代里的 `document.json` 是否在另一个进程被推进”。因此所有 identity、名称、ACL、config、collections 与 `_v` 消费必须经过 Document 的唯一 strict parser：轻量读取使用 detached `DocumentMetadata`，需要让验证与后续动作绑定同一内容时使用 `open_document_head`，并在动作结束前保持 `.ze-document.lock`。

Legacy scanner 的 source context 会把 `open_document_head` 绑定到 topology 已 pin 的 root identity，并把外层 content SH 保持到 capture 完成。这样同 generation 的普通 save 也不能让一次扫描把旧 head 与新 Entity generation 拼接；replace/delete 则更早被 repository/project lifecycle 共享锁挡在 quiesce/rename 之前。读侧发现 live root 不再是 owner-controlled real directory 或 named inode 已改变时，只返回 path-free 的 `project_recovery_required`（`live_scan_root_unsafe` / `live_scan_root_identity_mismatch`），不会借用 copy 写侧的 `project_topology_write_failed` 诊断。

普通 live namespace 操作的锁序唯一为 `repository → project lifecycle → Document content`，不能反向。legacy root migration 是一个被 repository EX 全局准入隔离的窄特例：它必须先锁旧根 head 才能取得 target id，随后在仍持 repository EX 时通过 reentrant `topology.create` 获取该尚未发布 target 的 project EX。这个 `repository EX → legacy content EX → target project EX` 次序不会与普通 topology 操作形成环，因为 repository EX 已阻止新的 topology 准入，target 也没有可消费 legacy content 的反向依赖；不得把这个特例推广到 live 项目路径。

- legacy migration 先拿 repository EX，再对旧根 Document 拿 EX+`missing_ok`，从 ID 解析、安全复制、目标发布到旧源清理始终在同一内容锁内。只有先行迁移在 bounded topology lock timeout 内完成时，并发第二个 bootstrap 才会等锁后看到 source 已消失并做 no-op；超过该窗口固定返回 `project_busy`，不会把暂态 `document_lock_unavailable`/`document_storage_unsupported` 误报为内容损坏。大体量旧根（上限可达 8 GiB）的启动等待、进度与可重试导入由后续 legacy importer 路线负责，当前版本不承诺无限等待。
- duplicate 同时锁住 source/target lifecycle，再对 source Document 拿 SH；授权回调、safe copy、staging metadata rewrite、完整验证与 target rename 都消费这一份 head。
- replace/delete 在项目 quiesce 后拿 lifecycle EX，再对即将移走的 live Document 拿 SH；名称/ACL 验证与 namespace rename 不允许跨两个 head。
- duplicate 的 metadata-only rewrite 不是普通编辑 API。topology 必须证明目标是自身 `.ze-staging` 下真实 direct child，operation ID 有 matching `prepared + duplicate` journal，并冻结打开 stage 的 `(st_dev, st_ino)`；Document/EntityFileStore 按路径重开根目录时必须再次匹配这一 inode pin，随后才可用私有 capability 做 version CAS、完整 head 重验与原子写。live 名称、ACL、config 编辑仍走 Command → Coordinator → OpLog/version。

`.ze-project.json` marker 是 namespace identity 断言，不只是目录标签。create/duplicate/replace 写 marker 前都在 Document SH 下要求 `head.id == project_id`；启动 reconciliation 对已有 marker/state 也重新验证 Document identity，错配项目立即 freeze 为 recovery required，不能因为控制文件彼此自洽就继续列为健康项目。

Repository 的 cache miss 在 `build_project_context` 完成后、写入 `_contexts` 前重新读取 durable metadata；若 open→registry/recovery 期间 head 已改变，先 retire 这个尚未发布 Context 的 supervisor，确认停止后原错误重抛，绝不缓存半旧对象。cache hit 返回前同样在 `ctx.lock` 下比较 namespace ID 与 `ctx.doc.version`。同 generation `_v` 漂移返回 `commit_repair_required`：不返回旧 Context、不 pop、不重建，也不自动 reload。这是有意的安全停止，不是缺少一个刷新调用。当前文件 adapter 的顺序仍是 Document commit 后再 append OpLog，跨进程观察者可能正好看到“新 Document + 旧 OpLog”；在二者进入同一个事务且 Context 有 retire/active lease 前，自动重载会把半提交状态升级成权威。另一个进程切换 topology generation 时，本进程当前只会在下次 `get_or_load` 移除旧 cache mapping，尚未统一 shutdown/retire 旧 supervisor；这属于后续 Context retire/active-lease seam，不得把“未返回旧 Context”扩写成“旧运行时已经完整退役”。

## 四种写操作

### Create

`prepared journal → unique staging → build/validate/fsync → stage→live → fsync 两侧父目录 → live state → committed journal → cleanup`

目标只有在完整校验并完成 rename 后可见。并发创建同一目标只能有一个赢家；失败方只清理由自己认领的 staging/journal。

### Duplicate

在 source/target lifecycle 锁与源 Document SH 内容锁下形成一致快照，但只复制业务正本：`document.json`、`entities/`、`assets/`。OpLog、pipeline attempt/status、export 和临时文件明确不属于副本。复制后只在 topology 证明的未发布 staging 内重写 ID、名称、ACL 与 intent marker，再按 create 的发布协议切换目标。

### Replace（Demo reset）

先完整构建新代，再将 state 设为 quiescing，停止缓存 Context/worker，取得项目独占 lease，把旧 live 停放为唯一 backup，最后把 stage 发布为 live。任一切换前失败恢复旧 state 并删除本次证据；旧 live 已离开原位后发生的异常只能返回 durability unknown 或 recovery required，不能假装“确定没写”。

首次创建 demo、替换现存 demo、从 deleted tombstone 恢复 demo 都由同一个状态机处理。旧 live 回滚已完成但尚未来得及清 journal 的崩溃窗口也有明确恢复分支。

### Delete

删除的逻辑提交点是 `live→trash + deleted tombstone`，不是 `rmtree`。只有 tombstone 和 committed journal 已耐久后才做物理 GC。GC 失败返回成功且 `cleanup_pending=true`；重启继续清理，不会把已经成功的删除误报为失败，也不会授权重复删除。

## 客户端意图与未知结果

每次真实用户动作至少有三个身份：

- `intent_id`：32 位小写十六进制，写入 topology marker/journal，证明业务操作本身。
- `Idempotency-Key`：不透明 HTTP 重放键，写入请求账本，证明同一网络请求。
- `principal_id`：由服务端认证会话或 HttpOnly guest cookie 推导，写入 journal/outcome，证明这是谁的操作；客户端不能自报或覆盖。

Create/Duplicate 还必须在发送前固定 `targetProjectId`。这些身份必须跨网络重试保持不变。客户端错误分类采用 fail-closed：只有服务端合同明确证明 `not_committed` 才能授权同 intent 的一次有界 replay；HTTP 408、网关错误、未知 4xx/5xx、响应合同不完整和 `idempotency_outcome_unknown` 一律先视为结果未知，只能查询：

- `GET /api/projects/{project_id}/intents/{intent_id}`
- `GET /api/demo-templates/{template_id}/intents/{intent_id}`

### Proof wire v2：观察范围与耐久认领分开表达

公开对账端点返回的最小证明如下：

```json
{
  "project_id": "p-01",
  "intent_id": "0123456789abcdef0123456789abcdef",
  "kind": "create",
  "status": "not_committed",
  "proof_revision": 2,
  "principal_scoped": true,
  "intent_reserved": false
}
```

- `principal_scoped=true` 只表示服务端已经按**本次请求的认证主体**检查证据；它不表示 GET
  已替这个主体占住 intent，也不等于“某个耐久文件里保存了主体绑定”。公开客户端只接受 true。
- `intent_reserved=true` 表示 exact `intent + principal + target + kind` 已有耐久、永久的身份认领；
  claim、匹配的 journal 或终态 outcome 都能提供这一事实。完整的提交前回滚会删除 journal，但保留
  claim，因此仍可能返回 `not_committed + intent_reserved=true`，让同一身份做有界重试。
- `intent_reserved=false` 表示这次共享锁快照里尚无 exact 耐久认领。它是**时点观察，不是租约**：
  GET 返回后到 POST 抵达前，另一个进程可能先认领同一 operation ID。POST 必须在 repo 独占锁内
  原子认领；竞争失败时客户端按 conflict/unknown 冻结并重新对账，不能把这次 GET 当写许可。
- `committed / deleted / in_progress` 是正向证据。公开 principal-scoped 查询若读到的
  outcome/journal 缺 principal hash，或者只能看到 marker/tombstone 而找不到任何主体认领，必须
  返回 `project_recovery_required`，不能把旧的无主体记录升级成当前用户的成功证明。
- `principal_bound` 已删除。客户端必须精确校验 revision 2、目标、kind、scope 与布尔型 reservation；
  缺字段、旧 revision 或多义值都属于不完整合同，按 outcome unknown 处理。

状态含义：

| 状态 | 含义 | 客户端动作 |
|---|---|---|
| `committed` | 主体绑定的 immutable outcome（或等强 journal/claim+marker）证明操作已提交 | 完成并打开目标 |
| `deleted` | immutable outcome 证明删除已提交 | 完成删除 |
| `in_progress` | journal 仍在，结果尚未收敛 | 等待后再次查询，不发新写入 |
| `repair_required` | 证据矛盾，自动判断会有数据风险 | 冻结该意图，提示修复 |
| `not_committed` | 当前 principal/target/kind 快照中无提交证据；`intent_reserved` 说明 exact 身份是否已耐久认领 | revision、scope、目标、种类都匹配后，才可用完全相同身份有界重试一次；false 时 POST 仍可能输掉原子认领竞争 |
| `conflict` | 目标属于另一代/另一意图 | 不得覆盖，要求用户换目标发起新动作 |

匿名 demo 的 guest cookie 必须在 mutation 前先到达浏览器。只读模板列表会建立 cookie；若客户端直接首发 POST，服务端只返回 `guest_session_required` 并下发 cookie，不创建 sandbox，客户端随后用原 intent/key 重试。sandbox ACL owner 必须精确等于登录 user ID 或原始 guest identity；`__demo__` 前缀绝不是授权，两个浏览器即使知道彼此 project ID 也不能读、写、重置或订阅对方沙盒。返回前端的 `guest_scope` 只是域分离哈希，用于隔离本地意图分区，不具有授权能力。

## 一次编辑的完整控制坐标

任何消费方若只保存一个 project ID，都不足以判断“这还是不是我看到的那份数据”。当前
文件兼容层的最小控制坐标是：

```text
principal_id
  + project_id
  + project_generation
  + document_version
  + event_epoch / event_cursor
  + operation_id (intent / idempotency / commit identity)
```

- `principal_id` 防止跨账号、跨 guest 认领同一请求；
- `project_generation` 防止 reset/delete 后旧 Context 写回；
- `document_version` 防止同代晚到 HTTP/GET 覆盖新内容；
- `event_epoch + event_cursor` 防止重启、断档与乱序被伪装成实时追平；
- `operation_id` 防止网络重试变成第二次业务动作。

项目列表也必须在 repo shared lock 下形成一个拓扑快照，每行带 generation；重复 project ID、
缺 generation 或无法验证的 ACL 行会让整份响应失败，不能让客户端从一份自相矛盾的列表里
任选一个版本。

## 启动恢复与失败隔离

Repository 在加载任何 Context 前先执行 topology initialize：

1. 逐 journal 获取对应项目独占 lease并恢复。
2. 根据 marker/state/journal 组合完成发布、回滚或提交后清理。
3. 已提交 journal 若缺 outcome，先补写不可变 outcome，再清 journal/backup/trash；outcome 与 journal/marker 矛盾则冻结，不能覆盖证据。
4. 清理无 journal 的 orphan staging。
5. 将无法证明归属的 trash、live/state 矛盾、孤立 quiescing 状态冻结为 `recovery_required`。
6. 旧格式项目只有在 document 可验证且 marker/state 同时缺失时才原子收养为 generation 1。

可识别到 project ID 的坏 journal、validator 异常或单项目 I/O 错误只冻结该项目，健康项目仍可启动。无法可信识别目标的控制面损坏才提升为全局启动错误。

## 可执行校验

- `sdk/test_project_topology.py`：真实多进程 flock、双写竞争、`os._exit` 崩溃窗口、fsync/rename/outcome 故障注入、跨代 intent 回执、generation fencing、symlink、canonical duplicate。
- `GATE-PROJECT-01`：代际推进、旧 Context fencing、删除 tombstone 对账。
- `GATE-PROJECT-02`：Repository/HTTP/CommitCoordinator/PipelineSupervisor 生产接线，防止未来代码绕过唯一生命周期 seam。
- `GATE-PROJECT-03`：副本只含业务正本。
- `GATE-PROJECT-07`：legacy migration / duplicate 的 symlink、hardlink、FIFO 对抗矩阵必须零发布、零外部改动、零来源清理。
- `GATE-PROJECT-08`：有限 AST 污点分析阻止 production 原始读取/解析 `document.json`；验证 marker/启动恢复的 Document identity fence、legacy/duplicate/replace/delete 的 head snapshot 覆盖、staging journal+inode proof，以及 cache miss/hit 的 `_v` fence 只 retire/fail closed、不自动 reload。
- `GATE-PROJECT-09`：全 repository barrier 的 SH/EX 准入、严格 live catalog 闭合与 fork child 只 close 不 `LOCK_UN`。
- `GATE-PROJECT-10`：唯一 trusted rollout caller、admission 三边崩溃恢复、全普通入口 pre-I/O 拒绝与 live+deleted generation high-water 闭合。
- `GATE-PROJECT-11`：同一 EX→EX 屏障覆盖 S0→v4/current projection→S1→v5 fresh reconcile→S2；已有 v5 时旧 v4 writer 不再运行，临时 plan lane 必须显式释放，且不得发生 admission CAS 或公共挂载。
- HTTP/frontend 测试：强制 principal+target+intent+key+expected generation、稳定重放、
  跨账号/跨 guest 拒绝、guest 预建身份、未知结果对账、snapshot→SSE cursor 闭合、刷新失败
  不误清项目。

## 明确边界

当前实现承诺同一主机、本地支持 `flock` 的文件系统上，多进程互斥、目录切换恢复与旧 Context fencing。部署进程必须是**合作且受信任的同一运行身份**：数据根不能同时开放给会恶意 rename/替换 `.ze-topology` 父目录的同用户代码；`O_NOFOLLOW`/树校验能拒绝已出现的 symlink 与特殊文件，但不把“攻击者并发改写锁目录”包装成安全隔离。需要隔离不受信任插件时，应使用独立系统用户/容器和只读代码目录。

它不把 JSON state/journal/outcome 描述成跨设备云事务，也不负责业务文档内容的多主合并。尤其是 Document 与 OpLog 仍按两个 commit point 顺序落盘：本页的 content snapshot 只保证一次受锁 topology/head 操作不混用多个 Document head，cache fence 只在实际观察到同 generation `_v` 漂移后停止返回旧 Context；它不能证明 cache miss 开始前就存在的“新 Document + 旧 OpLog”是否完整。因此这里**没有**提供跨进程自动 reload、原子业务历史或 durable event outbox。私有 metadata capability 只约束合作代码的架构入口，不抵御能在同一 Python 进程任意 import/反射私有对象的恶意代码。

文件 outcome 当前保留到 W06 迁移，空间随已提交拓扑操作线性增长；不能在缺少客户端 intent 生命周期与数据库归档策略时擅自 TTL 删除。W06 会把 catalog、operation ledger、business commit、outbox 与 action journal 放入事务数据库；W07/W08 再接离线同步和 durable realtime。升级时保留本页的四个外部不变量：稳定 intent、单调 generation、activity lease、可查询结果证明。
