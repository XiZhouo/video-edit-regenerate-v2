# Document — skill 的数据根

## 1. 这是什么

每个 ZE 工具启动后，用户能编辑 / 保存的所有东西都挂在一个**根**上——这就是 Document。没 Document 就没项目，没项目这个工具只是个空壳。

业务白话视角：业务同事打开 ZE-seek-find-object-generator 看到的整个工程（里面装着多个关卡 entity）、打开 ZE-knowledge-base 看到的一个知识合集——都是一个 Document。它装着这个工具的所有数据，决定能存什么类型的 entity、字段长啥样、是不是只读外部依赖。

默认一个打开的工作对象是一个 Document。对于 seek-find 这类"一个项目里管理很多关卡/背景"的工作台,批量项目应使用 `DocumentIndex` 引用多个独立 Document,而不是把所有关卡揉成一个巨型 Document。

新建 / 打开 / 保存 / 关闭 / 导出，操作的都是 Document。

## 2. 数据目录约定

数据**不存 skill 内部目录**，存在 skill 的**兄弟目录** `<skill>-data/`：

```
~/.claude/skills/
├── ZE-foo/                ← skill 代码（git 仓库）
│   └── ...
└── ZE-foo-data/           ← skill 数据（兄弟目录，不进 git）
    └── projects/
        ├── <doc_id>/      ← 一个 Document 一个目录
        └── ...
```

**约定**：
- 路径算法：`dirname(skill) / (basename(skill) + "-data")`
- 环境变量覆盖：`ZE_DATA_DIR=/path/to/data`
- 缺失父目录从最近一个 owner/mode 安全的现存祖先 fd 起逐组件 `mkdirat(0700)+openat/fstat+fsync`；中间 symlink 或共享 writable 祖先拒绝，不使用 `Path.mkdir(parents=True)`
- skill 工程层（`server.py`）决定 path；Document 类不关心 path 在哪，只接受调用方传进来

**为什么是兄弟目录**：skill-store 更新会 rmtree 整个 skill 目录，数据写 skill 内部会丢；同时多 skill 并存时数据隔离清晰。

**实施位置**：`server.py:27` —
```python
DATA_DIR = Path(os.environ.get("ZE_DATA_DIR", ROOT.with_name(ROOT.name + "-data"))).expanduser()
```

## 3. 物理布局

每个 Document 落到磁盘是 `<skill>-data/projects/<doc_id>/` 下 3 个子项：

```
projects/<doc_id>/
├── .ze-document.lock  ← 0600 稳定锁 inode；open=SH，save=EX（有界等待）
├── document.json       ← 唯一权威 commit point：索引 + generation + 元数据
├── entities/
│   └── .ze-store-v2/
│       └── <document_version>/
│           └── <digest[:2]>/<digest>.json
└── assets/             ← 大文件，内容寻址（AssetStore 管）
    ├── blobs/<sha[:2]>/<sha><ext>
    └── meta/<sha>.json
```

`digest` 不是字符串拼接，而是固定协议：

```
SHA256(
  b"ze.entity-file.v2\0" +
  uint64_be(len(type_utf8)) + type_utf8 +
  uint64_be(len(id_utf8)) + id_utf8
).hexdigest()
```

`document.json.entity_storage` 必须精确等于
`{"schema":"ze.entity-files.v2","layout":"document-version/shard/sha256-v1"}`。
因此任意合法 UTF-8 type/id（包括 `/`、`..`、绝对路径外观、Unicode 与长字符串）只参与
hash，不会成为路径。保存先把新 `_v` 的**完整** generation 写完并逐层 fsync，最后以随机
`O_EXCL` 临时文件替换 `document.json`；只有这次 rename 改变权威 head。成功后旧 generation
与 legacy raw tree 才是可清垃圾，清理失败不能把已提交 head 伪报成失败。

**为什么这么拆**：
- entity 单独存：document.json 不膨胀；generation 让旧 head 永远只引用完整旧集合，消除“新索引引用半写 entity”窗口
- asset 内容寻址：同一文件被多 entity 引用只存一份，自动 dedup；引用计数支撑 GC

**legacy 兼容与安全边界**：无 `entity_storage` marker 的旧文件仍可读
`entities/<type>/<id>.json`。type/id 必须是 strict UTF-8、非空、拒 `/`/NUL/`.`/`..` 的单
POSIX 组件；type 额外保留 `.ze-store-v2`，id 的 `.ze-*` 不与控制目录冲突所以继续兼容。
Unicode、空格和文件系统实际可承载的长名称不被人为缩成 ASCII/128 字符子集。既有
owner-owned `0644` 文件和 `0755` 目录可读；group/world writable、
wrong-owner、symlink、hardlink、FIFO/device/socket 等一律拒绝。下一次成功 save 自动迁到 v2，
新文件固定 `0600`、managed 目录固定 `0700`。所有遍历/读写/清理使用 dirfd、`O_NOFOLLOW`、
`fstat`，不在 managed tree 内用 `Path.exists/read_text/iterdir/is_file`。支持 Python 3.9+ 的
macOS/Linux POSIX API，不依赖 Linux-only syscall；同 UID 恶意进程并发替换更高祖先不在
本叶子的沙箱承诺内，由 ProjectTopology 的 namespace fencing 管理。

**跨进程一致性**：项目根内固定 `.ze-document.lock`（owner、`0600`、单硬链、
`O_NOFOLLOW`）是 Document 文件协议的一部分。`open` 从读取 `document.json` 到最后一个
Entity 恢复完一直持有 shared `flock`；`save` 从 durable-head CAS 到 generation 写入、
`document.json` commit point 和 cleanup 完成一直持有 exclusive `flock`。锁等待有界，超时
固定 `document_lock_unavailable`。所有 store read 方法 assert 至少 SH，audit/write/commit/
cleanup assert EX，不能依赖 caller 自律。锁内 save 必须严格解析现有 head，要求 exact `id`
且等于内存 Document，并比较磁盘 `_v == self._v`；不等时在触碰同名新 generation 之前返回
sealed、可证明 NOT_DURABLE 的 `version_conflict`。root-owned sticky `/tmp` 可作为路径 trust
anchor；非 sticky writable 祖先继续 fail closed。

metadata/topology consumer 不得另开 `Path.read_text + json.loads` 旁路。`Document.open`、
save 的 durable-head CAS、`read_document_metadata` 与 `open_document_head` 共用唯一 strict head
parser，并在同一安全 root dirfd + project flock 下验证全部 head 元数据。context 交付锁内读取的
exact immutable bytes；snapshot copy 的 verify、copy、publish 必须留在 context 生命周期内，不能
退出后按 Path 重读。duplicate source 持 SH；会在发布后清理 legacy source 的迁移持 EX，并用
`missing_ok=True` 把“等待先行进程后发现 source 已清理”解释为成功 no-op。metadata-only rewrite
只允许 topology coordinator 对已证明位于 staging root 且拥有 prepared duplicate journal 的
unpublished head 使用私有 capability；live metadata 变化仍只能走 Command → Coordinator → OpLog →
`Document.save`。capability 与 carrier seal 是同一受信 Python 进程内、由静态 gate 约束唯一生产
消费者的防误用边界，不是抵抗同进程恶意 import/反射的安全沙箱。
topology 可把外层已固定 live root 的 `(st_dev, st_ino)` 通过 `open_document_head` 的私有参数传真
给 store；store 必须在自己的 held root fd 上复核，不能把一个后来替换、但 owner/mode 仍合法的
目录误当原 root。strict legacy scanner 使用独立 module capability 守卫的私有 context，并与
`Document.open` 共用同一个 locked-store Entity loader：exact capability 与 exact root identity 必须
在任何 store/I/O 前成立；随后一个 store/root fd/SH 覆盖 head、v2 或 raw legacy Entity、完整
Document 重建以及调用方留在 yield 内的 snapshot capture。context 只交付 sealed `DocumentHead`
与 exact `Document`，不得交付内部 mutable parsed-head graph；退出即关闭 lock/root fd。scanner
在读取第一行前预检 Entity 总数不得超过 1,000,000；加载 raw Entity bytes 的聚合上限为 1 GiB，
每行必须把剩余预算传给 file stat/read 边界，在分配该 payload 前拒绝超限。超限固定 path-free reason
`legacy_scan_entity_count_exceeded|legacy_scan_raw_entity_bytes_exceeded`；这些私有门限不改变普通
`Document.open` 的兼容合同。
无 v2 storage marker 的 collections 还必须在这个共享 parser 内复用 legacy type/id POSIX component
校验（包括空 bucket 的 type 与 `.ze-store-v2` 保留名）；不得等到 Entity I/O 才让 full open 单独拒绝。
完整 exact payload 只由 `DocumentHead` 持有；`DocumentMetadata` 把六个已验证 nested member 分别
固化为 immutable compact fragment bytes，getter 只 strict-decode 对应片段并返回 detached graph。
因此 ACL/config 热读不会为无关 collections/unknown extension 重复解析整份 64 MiB head；top-level
unknown member 仍完整保留在 `DocumentHead.payload/envelope`，缺失 ACL/config 的 `{}` 语义不变。
兼容旧 head 缺时间戳时采用确定性规则：两者都缺为 `0.0/0.0`，只缺一个则继承另一个，纯读取
不得调用 `time.time()` 让同一 durable bytes 在不同进程产生不同事实。

**不由 Document 叶子拥有的物理数据**：
- durable OpLog、commit journal、outbox 与恢复工作副本由 `02-data-flow/07-persistence`
  及 `03-commands-operations/02-operation` 管理。Document 只表达 canonical 数据根，不能
  自己偷偷再造一套提交顺序。

## 4. 生命周期

```
   create(schema)
       ↓
      open  ←──────┐
       ↓           │
     edit──────────┤   mutation session 内 add / update / delete
       ↓           │   (先改内存，由 CommitCoordinator 决定 commit/rollback)
       │           │
       ├── save ───┤   完整写/fsync 新 generation，最后 rename document.json
       │           │   唯一 commit point；bump _v；409 if expected_version 不匹配
       │           │
      close        │
       └───────────┘
```

**三种协作模式**（必显式选）描述协作拓扑，而不再兼任保存开关：
- `with_save`：历史模式名，继续接受以保持兼容；新代码不要从名称推断保存时机。
- `live_collab`：多人实时协作。
- `realtime_checkpoint`：带 checkpoint / operation group 的协作语义。AI task 属于 Document，不随浏览器 session 关闭而取消。

**保存策略**是独立的可选声明：
- `auto_commit`：成功 mutation 后自动提交 canonical。
- `checkpoint_commit`：同样自动提交，并明确要求提交协调器保留 checkpoint 语义。
- `legacy`：只保留旧声明的可读性，不代表手动保存；实际安全收敛到 `auto_commit`。

旧运行时曾允许 `live_collab` / `realtime_checkpoint` 成功 mutation 后只落 OpLog、不落
Document。这会在重启后留下不可安全撤销的分叉，因此旧文档缺 `save_policy` 或显式声明
`legacy` 时，不论 collaboration_mode，实际策略都按 `auto_commit` 执行。

新工具应显式声明 `auto_commit` 或 `checkpoint_commit`。旧 `document.json` 没有该字段时，
`Document.open()` 保持声明值为 `None`；普通 `save()` 仍不写该字段，避免无意迁移。框架不提供
manual-save 策略；草稿、版本、发布、另存与导出是独立产品动作，不复用保存开关。

**409 冲突检测**：save 先校验客户端 `expected_version` 与内存 `_v`，再在 exclusive 锁内
比较 durable head `_v` 与内存 `_v`；任一不一致均抛 `ZeError("version_conflict")`，业务方
决定 merge / reject。durable-head 冲突是 module-sealed 的 pre-commit outcome，FileCommitStore
只能识别该精确证明为 NOT_DURABLE，不能依据任意同名 code/context 猜测。
`_v` 与显式 `expected_version` 都限定为 exact `int` 的 `1..2^63-1`；不接受 `bool`、
float、int 子类或自定义 equality 对象。版本已到上限时，rename/set_acl/save 在修改内存或
磁盘前以 `version_overflow` 拒绝，确保当前状态始终可进入 SQLite INTEGER。

### 内存 mutation session

Command handler 会先就地修改 Document/Entity，再返回 Operation。持久写在 handler 之后才
可能失败，因此协调器必须在执行前调用 `begin_mutation_session()`：

- Document + OpLog 都可证明 durable：`session.commit()`，保留内存修改；
- 尚未发生 durable 写且提交失败：`session.rollback()`，精确回到执行前；
- 写入结果 unknown / 已 durable：不能擅自 rollback 后重放，按 `CommitReceipt` 进入修复。

snapshot 绑定**同一个 Document 对象身份**。即使两份加载对象的 `document_id` 相同，也
不能互相 restore。恢复不会替换 Document；对于快照前已存在的 Entity，也恢复原对象的
完整 `__dict__`，因此编辑器、缓存或命令代码持有的外部引用不会悬空。会话中新建的 entity
被移除，hard-remove 的原 entity 会原对象复位。

`commit()` 与 `rollback()` 都是 one-shot terminal transition。已经 committed、rolled_back
或 rollback_failed 的 session 再次 finalize 会抛 `mutation_session_closed`，防止旧回滚
凭据撤销后续提交。协调器外层 session 与 command 内层 session 可以嵌套：内层 finalize
只关闭内层，外层仍能回滚整批；同一线程必须按 LIFO 关闭，越过仍 active 的内层会话会
抛 `mutation_session_order_violation`。

`begin_mutation_session(exclusive=True)` 是未发布 runtime head 的更窄模式：进程内任何已有
session 都会使它以 `mutation_session_busy` 失败；exclusive active 时也拒绝后来 session 和
跨线程重叠。它还在任何路径/锁文件 I/O 前禁止同一 live Document 的 `save()`，避免 migration
或 loader 绕过冻结 snapshot/authority adapter 把临时态写进旧文件 authority。普通同线程
session 的既有 LIFO 嵌套保持不变。exclusive 只是一道进程内防误用边界，仍要求外层持项目
generation/load lease，并不能阻止 callback 私自写其他文件或网络。

W04 实现采用全量 checkpoint，capture/restore 的时间与内存为 O(全部 Document 元数据 +
全部 Entity state)。这是正确性基线，不是大文档最终性能目标。当前命令能直接调用
`Entity.update/delete`，Document 无法可靠观察每个 before-mutation，所以不能用会漏记的
“伪增量”优化。统一写入口和 Entity hook 建立后，内部应换成按触达对象 journal，同时保持
本节公开 API 不变。

### 类型级 runtime mutation policy

`DocumentRegistry.declare_entity_type` 的可选 `mutation_policy` 把“这个类型可以被哪些业务意图怎样修改”变成运行时不变量：

#### 声明语法是闭合合同

- 顶层只能精确为 `schema/lifecycle/fields`，或再加唯一可选的 `cardinality`；未知键、缺键和非 exact JSON 值都拒绝。
- `schema` 固定为 `ze.entity-mutation-policy.v1`。
- `lifecycle` 必须精确包含 `create / rename / soft_delete / restore / hard_remove` 五个键。每个值都是 command id 数组；允许空数组，表示这个生命周期没有任何合法业务入口。
- `fields` 只能引用该 type canonical schema 的顶层 `properties`。每个被列字段的 command id 数组必须非空；schema property 或 policy field 不能含 `.`，也不能使用 `__entity__ / __deleted__ / __restored__`。未列出的 schema 字段默认不可写。
- command id 必须是非空合法 UTF-8 字符串，最长 256 个字符，不含 C0/DEL 控制字符；同一数组内不允许重复。
- `cardinality` 出现时只能精确为 `{"max_total": N}`；`N` 必须是 exact int（`bool` 不算），范围 `1..2^63-1`。

声明只决定“允许谁做什么”，不能单独证明命令真的存在。framework 与 mod 命令全部注册后，bootstrap 必须在开放请求和 `PipelineSupervisor.recover()` 前调用
由 framework bootstrap 调用私有 `doc._validate_mutation_policy_commands(command_registry)`；mod/downstream 不直接消费这项 authority。每个引用不仅要 `has(command_id)`，还要在注册时显式声明 `mutation_authority=True`。注册表以注册瞬间冻结的 framework-owned execution/security snapshot 为权威；注册后修改公开 `CommandSpec` 不会获得权限，篡改绑定会 fail closed。通用 `entity.*` helper 默认且固定 `False`，不能直接写进 policy 充当业务根。

runtime authority 的生命周期也是合同，而不是普通可变配置：当前代码声明只能由 framework 私有 bootstrap/runtime-rehydration seam 安装到**尚未发布**的 Document；完整 registry 校验成功后，Document 单向进入 sealed 状态。seal 后再次安装、替换 policy，或在命令中篡改 policy/seal/schema/collection authority，都会在触达 store 前拒绝并恢复。未 sealed 的窗口只服务新建 seed、只读源物化和受控 legacy migration 准备，不是 public session，也不能被业务 caller 当作绕过命令授权的模式。

sealed 之后，受保护类型的 `Entity.update/delete/restore`、`Document.add/hard_remove` 会在底层入口再次要求两份同时成立的运行权威：外层 CommitCoordinator coverage，以及当前真实业务 command segment 的 authority；history replay 使用 coordinator-owned replay coverage 取代业务 command authority。也就是说，即使 caller 拿到了 Python 对象和公开 CRUD 方法，public pre-session/direct write 也会在变化前 fail closed。这个 fence 防的是正常框架路径中的同进程误用和旁路，不宣称把任意可 import 私有符号、可篡改进程内存的 hostile Python 变成安全 sandbox。

#### 授权看真实变化，不看文字声明

- 未声明 policy 的类型保持旧行为；同一 Document 中只有真实触碰受保护类型，或返回 patch 声称触碰受保护类型的 command segment，才启用严格路径。仅仅设置 `mutation_authority=True` 不会改变未受保护类型的兼容语义。
- 权限身份是最外层 dispatch 的原始业务 command id。业务根可以在 handler 中嵌套 dispatch 通用 helper，但必须保留同一个 registry、Document、actor 与 source；helper 的 id 既不升权，也不能被外层丢掉、替换或重排。
- create 是组合授权：必须同时拥有 `lifecycle.create`；若新 Entity 的 metadata `name` 非空，还必须拥有 `lifecycle.rename`；初始 `data` 中每一个实际字段都必须拥有对应 field 权限。不能只给 create 权，再借初始 payload 灌任意字段。
- dot-path（如 `box.a`）按第一个顶层字段 `box` 查权限；审计 Patch 与 AI provenance 仍保留原始路径 `box.a`，不能把两个子路径混成一项。
- read-only source 是不可授权的 overlay：业务命令无论 policy 怎样写都不能创建、修改、删除它，也不能把它计入 `max_total`。framework 可在尚未发布的 bootstrap 阶段从声明源重新物化 overlay；canonical 软删行则是耐久 tombstone，仍计数。
- 受保护实体的 actor log、所有返回 Operation 的 `actor/source` 都必须逐字等于 dispatch 开始时冻结的 Actor 与 source，且 `Actor.source == dispatch source`。poll、governor、handler 或嵌套 callback 修改 Actor 会回滚并拒绝。

#### Operation 必须是可正放、可倒放的精确程序

framework 从 mutation snapshot 计算真实 graph delta，并要求本次全部 forward/reverse patches 对受保护类型的目标、操作、顺序和值精确一致：forward 必须把 snapshot 变成 final，reverse 必须从 final 回到 snapshot。漏报、伪报、重复、丢弃 nested Operation、前后值造假或只改数据不留审计都会在触达 store 前回滚。

- JSON `null` 与字段不存在不是一回事。新增一个此前不存在的字段时，forward 是 `set`，reverse 必须是 `delete`；显式写入 null 仍是 `set(..., after=None)`，reverse 也仍是 `set`。policy opt-in 的 history `delete` 真正移除字段；未采用 policy 的旧文档继续保留历史 set-to-null 兼容语义。
- 多 patch 是有序程序。reverse 不只是逐项交换 before/after，还必须把整段顺序倒过来；否则同一路径的连续写入、批量创建或父子路径更新无法精确恢复。
- dot-path 的父链原来是 missing、null 或 scalar 时，仅靠子值 before/after 不够；Patch 的 `ancestry={field,before:{present,value},after:{present,value}}` 保存顶层根的存在性和值，reverse 交换两个端点。
- AI provenance 使用同形状的 `provenance` 信封，但 `field` 是原始 dot-path；reverse 同样交换端点。未绑定到本次真实 patch path 的 provenance 拒绝。
- 受保护的 create / hard-remove Patch 必须携带精确非负 `position`，指 canonical bucket 中的位置；forward/reverse 使用同一位置，恢复时 read-only overlay 仍固定在 canonical 段之后。生命周期 Patch 的 before/after 是完整 Entity envelope，不能只给 id 或伪造版本。

#### 最终态、no-op 与运行时安装

- `cardinality.max_total` 是最终 canonical 行数上限；命令、整笔 coordinator producer、history replay、runtime migration、新项目/demo seed 与 bootstrap 恢复都必须在发布/持久化前通过。当前只证明单进程/文件权威内的最终图；跨进程 singleton 仍必须由 W06 数据库唯一约束/事务承接。
- idempotent delete/restore 即使发现状态已经满足，也必须先执行 CAS 与 read-only 校验；合法 no-op 返回空 patches，Entity version 保持不变。不要把普通 `entity.update` 的同值写入一概叫 no-op：它仍可能是一次真实 CAS mutation，并增加 Entity version/actor log。
- policy 只属于当前部署的 runtime declaration，不进入 `document.json`、business snapshot、Entity 或 Operation。重启由 bootstrap/runtime rehydration 从代码声明重装；只改 policy 可以改变 declaration digest，但不改变业务 root、Document version/head，不产生 Operation，也不调用 authority writer。
- poll/governor/确认准备、授权失败、coverage 失败、cardinality 失败和 pre-store 提交失败都在 mutation session 内恢复对象图；不会留下半改数据、版本、OpLog 或 writer 调用。

这套约束当前保证单进程内真实 Document 图；跨进程同时创建的全局 singleton 仍必须由 W06 事务存储的唯一约束承接，不能把进程内 `max_total` 外推为多进程证明。

## 5. document.json 字段定义

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✓ | doc_id，全局唯一（uuid4 hex 默认）|
| `name` | string |  | 人看的名字 |
| `schema_version` | string | ✓ | 改 schema 时迁移用；不写不让 save |
| `collaboration_mode` | enum | ✓ | `live_collab` / `with_save` / `realtime_checkpoint` 必选其一 |
| `save_policy` | enum |  | `auto_commit` / `checkpoint_commit` / `legacy`；旧文档可缺失且普通保存不补写 |
| `data_schema` | object | ✓ | mod 端注册的 entity_type → schema 表 |
| `read_only_sources` | list |  | 只读外部源声明：`[{name, entity_type, loader_path, schema}]` |
| `acl` | object |  | document 级权限：`read` / `write` / `admin` |
| `config` | object |  | 业务自定义配置（画布尺寸 / 项目设置等）|
| `collections` | object | ✓ | `entity_type → [entity_id]` 索引；entity 本体在 entities/ 不在这里 |
| `entity_storage` | object | v2 ✓ | 精确声明 `ze.entity-files.v2` + `document-version/shard/sha256-v1`；缺失只表示 legacy raw layout |
| `created_at` | float | ✓ | 创建时间戳 |
| `updated_at` | float | ✓ | 最后修改时间戳 |
| `_v` | int | ✓ | 文档版本号，每次 save +1（409 用）|

## 6. Python API

每个方法标 ✅（已实装）或 ⏳（待补，标注被依赖的叶子）。规则：没跨叶子依赖的全部实装；真依赖别的叶子的才占位。详见 §11。

```python
from document import (
    Document,
    DocumentHead,
    DocumentMetadata,
    DocumentMutationSession,
    DocumentMutationSnapshot,
    DocumentRegistry,
    ReadOnlySourceSpec,
    open_document_head,
    read_document_metadata,
)

# ──────── Document — 元数据 + 持久化（全部已实装 ✅）────────

# 构造
doc = Document(
    schema_version="1.0.0",           # ✅ 必填非空，不写抛 ZeError
    collaboration_mode="live_collab",  # ✅ 必填: "live_collab" / "with_save" / "realtime_checkpoint"
    save_policy="auto_commit",         # ✅ 可选声明；新工具应显式选 auto/checkpoint
    name="my-project",
    id=None,                          # 默认 uuid4 hex
    schema={...},
    mutation_policies={...},          # ✅ runtime-only；缺省 {} 保持旧行为
    config={...},
    acl={...},                        # ✅ document 级权限占位 dict
)

# 元属性（只读 property）
doc.id / doc.name / doc.schema_version / doc.collaboration_mode  # ✅
doc.save_policy / doc.effective_save_policy                      # ✅ 声明值 / 兼容计算值
doc.auto_commit_enabled / doc.should_auto_commit                  # ✅ 只读自动提交判断
doc.adopt_save_policy("auto_commit") -> bool                      # ✅ bootstrap 迁移缺失/legacy 声明
doc.schema / doc.config / doc.acl                                # ✅
doc.mutation_policies                                             # ✅ detached runtime 声明
doc.version / doc.created_at / doc.updated_at                    # ✅

# framework 私有 authority（不属于下游 Document API）
doc._validate_mutation_policy_commands(command_registry)          # 启动前引用闭包
doc._validate_mutation_policy_cardinality()                        # 最终 canonical 上限

# 只读源
spec = ReadOnlySourceSpec(name, entity_type, loader, schema)
doc.register_read_only_source(spec)        # ✅ 记录 spec + 调 loader 灌 read_only entity
doc.read_only_sources() -> [str]            # ✅ 返回已注册的 source 名清单

# 持久化（每次 save 自动 +1 version）
doc.save(path)                                     # ✅ 元数据 + hash generation；document.json 唯一 commit point
doc.save(path, expected_version=N)                 # ✅ 409 冲突检测：不等则抛 ZeError("version_conflict")
Document.open(path) -> Document                    # ✅ 读元数据 + 重新加载 entities/

# framework 安全 metadata/head 读取（零 Entity I/O）
read_document_metadata(path) -> DocumentMetadata   # ✅ 短 SH；结果无嵌套可变别名
with open_document_head(
    path,
    exclusive=False,   # duplicate source=SH；破坏性 legacy migration=EX
    missing_ok=False,  # migration 并发 no-op 时才设 True
) as head:                                         # ✅ DocumentHead | None
    head.payload                                    # 锁内读取的 exact immutable bytes
    head.envelope                                   # 全 head detached strict-JSON graph
    head.metadata                                   # 已验证 detached metadata

# topology 内部另有 capability-guarded staging-only metadata rewrite：
# 仅 id/name/acl/config；expected-version CAS；保留 _v/timestamps/index/marker/unknown。
# 它不属于 public/live mutation API，不从 sdk 导出；live 变化必须走 command/coordinator。
# strict legacy scanner 内部另有 capability/root-identity guarded context：
# 同一 store/root fd/SH 内 yield exact head + exact Document，snapshot capture 必须在 with 内完成；
# Entity count > 1,000,000 或 raw aggregate > 1 GiB 时在行读取/分配前 fail closed；
# 普通 Document.open 不受这些私有门限影响。

# framework mutation transaction（必须在共享 mutation lock 内使用）
snapshot = doc.capture_mutation_snapshot()         # ✅ 全量 opaque checkpoint，绑定当前对象
doc.restore_mutation_snapshot(snapshot)            # ✅ 原地精确恢复；跨 Document 拒绝
session = doc.begin_mutation_session()              # ✅ capture + one-shot 状态机
session.state / session.active                      # ✅ active/committed/rolled_back/rollback_failed
session.commit()                                    # ✅ 保留 mutation，关闭凭据
session.rollback()                                  # ✅ 原地恢复，正常返回 None；失败抛 mutation_rollback_failed

# ──────── Entity 集成（全部已实装 ✅）────────

doc.add(entity) -> Entity                                                 # ✅ 加 entity；id 冲突抛 ZeError
doc.get(entity_type, entity_id) -> Entity                                 # ✅ 不存在抛 entity_not_found
doc.find(entity_type, entity_id) -> Entity | None                         # ✅ 不存在返回 None
doc.list(entity_type, include_deleted=False) -> [Entity]                  # ✅ 默认过滤软删
doc.types() -> [str]                                                       # ✅ 当前有 entity 的 entity_type sorted
doc.hard_remove(entity_type, entity_id)                                   # ✅ 物理删, 不查 read_only

# 引用图
doc.detect_cycles(edge_lookup=None) -> [[str]]                            # ✅ DFS 找环, 默认扫 $ref
doc.cascade_delete(type, id, *, actor,
                   edge_lookup=None, on_asset_unref=None) -> [(type,id)]  # ✅ BFS 软删 + asset unref hook

# ──────── DocumentRegistry — mod 端注册收集器（已实装 ✅）────────

registry = DocumentRegistry()
registry.declare_entity_type(
    name=name, schema=schema,
    collaboration_unit=False,
    mutation_policy=None,
)                                                                    # ✅ policy 缺省兼容
registry.set_collaboration_mode("live_collab")                          # ✅
registry.set_save_policy("auto_commit")                                 # ✅
registry.declare_read_only_source(name, entity_type, loader_path)       # ✅
registry.set_demo_project(name, bootstrap=lambda doc: ...)              # ✅
registry.build_document(schema_version, name="", ...) -> Document       # ✅ 用收集到的声明造 doc
```

## 7. Mod 端必填 + 注册 API

Document 是 core 给的**通用容器**——core 不知道这个 skill 有什么 entity 类型，**必须由 mod 端声明**。

**mod 业务方必须做的 5 件事**：

1. **声明本 skill 有哪些 entity_type** — Document 桶名清单
2. **声明每个 type 的 schema** — `data` 字段类型 / 必填 / 约束
3. **选 collaboration_mode** — 协作拓扑，不能用来表达保存时机
4. **新工具显式选 save_policy** — `auto_commit` 或 `checkpoint_commit`
5. **提供 demo 项目** — 启动后第一次进去能看到的样例

可选：
6. 声明 `read_only_sources`（如有外部只读依赖）
7. 为需要强字段、生命周期或单例控制的类型声明 `mutation_policy`，并在 CommandRegistry 中注册对应的显式业务权限根

**mod 注册 API**（在 `02-mod/05-code/_placeholder.py` 实现）：

```python
from sdk import DocumentRegistry

def register(registry: DocumentRegistry) -> None:
    # 1. 必填：声明每种 entity_type
    registry.declare_entity_type(
        name="object",                    # mod 自定义类型名
        schema={                          # field_types 校验规则
            "name": {"type": "string", "required": True},
            "bbox": {"type": "list", "items": "number", "length": 4},
            "image_ref": {"type": "asset_ref"},
        },
        collaboration_unit=True,          # 协作粒度（CRDT 用）
        mutation_policy={                 # 可选；一旦启用即闭集
            "schema": "ze.entity-mutation-policy.v1",
            "lifecycle": {
                "create": [], "rename": [], "soft_delete": [],
                "restore": [], "hard_remove": [],
            },
            "fields": {"name": ["object.update-name"]},
            "cardinality": {"max_total": 1},
        },
    )

    # 2. 必填：声明协作模式
    registry.set_collaboration_mode("live_collab")  # 或 "with_save"

    # 3. 新工具显式声明保存策略（无 manual-save 模式）
    registry.set_save_policy("auto_commit")  # 或 "checkpoint_commit"

    # 4. 可选：声明只读源
    registry.declare_read_only_source(
        name="external_levels",
        entity_type="level",
        loader_path="external/levels.json",
    )

    # 5. 必填：声明 demo
    registry.set_demo_project(
        name="入门 demo",
        bootstrap=lambda doc: [
            doc.add(Entity(type="object", data={"name": "样例物体"})),
            # ...
        ],
    )
```

`DocumentRegistry` 是新增 class（详见 §11 实施 roadmap），由 `ze_command` 启动时实例化并收集所有 mod 模块的注册，创建 Document 时把累积的 entity_type / schema / mode 灌进去。

mutation policy 引用的是 CommandRegistry 中**注册完成且 `mutation_authority=True` 的业务根**，不是这里的字符串自证。正确启动顺序是：收集 Document 声明 → 构造/打开 Document 并在未发布准备态安装声明、完成 seed/受控 migration → 注册 framework 与 mod commands → framework bootstrap 调私有 `_validate_mutation_policy_commands` 单向 seal → 再做 runtime recover 和开放请求；mod/downstream 不直调内部 authority。完整可运行的业务根嵌套通用 helper 示例见 [README](./README.md#怎么用) 与 [Command SPEC](../../03-commands-operations/01-command/SPEC.md)。

## 8. 红线规则

- **`document.json` 不能含 entity 本体或 binary**——entity 必须单独存 v2 hash generation（AP-01）
- **业务 identity 永不成为 v2 路径组件**——v2 只用固定 framed hash；raw type/id 仅在 legacy 以 strict UTF-8 单 POSIX 组件安全读取，下一 save 必迁移
- **`document.json` rename 是唯一 commit point**——entity generation 必须先完整耐久；commit 后只清不可达垃圾，不能因 cleanup 失败回滚/伪报权威 head
- **managed tree 禁止便利 Path API**——必须使用 held dirfd + `O_NOFOLLOW` + `fstat`；拒 symlink/hardlink/special/wrong-owner/group-world-writable，公开错误不得带绝对路径或 identity
- **必须有 `schema_version`**——没有不让 save
- **一律单 Document**——不允许一个 skill 实例同时管多份独立项目；要跨项目走 cross-skill-call（AP-02）
- **`read_only_sources` 来的 entity 自动标 `_read_only=true`**；dispatcher 拒绝任何写它的 Command（Gate 37）
- **只读物化永不进入 canonical save**：source entity 只作为当前会话 overlay 参与查询/展示，`Document.save()` 的 `collections` 索引与 `entities/` 文件必须过滤 `read_only=True`；源声明仍写入元数据供下次 bootstrap 重物化。旧版本误落盘的只读行在下一次保存时按 orphan 清理。
- **只读物化不得覆盖 canonical identity**：source schema 与 loader row 都只接受 exact built-in JSON 值并递归复制，不调用扩展对象的 deepcopy hook；数字必须有限、字符串/key 必须是合法 UTF-8、值图不得循环、业务值容器深度不得超过 120，别名展开后不得超过 250,000 个 value/key 节点。row 的显式 `id` 必须是非空字符串（缺失/`None` 才自动生成），`name` 必须是字符串，显式 `data` 必须是对象，禁止 Entity 构造器静默转型。loader 可复用嵌套缓冲。整批构造校验后才一次发布，loader 失败、非法值、批内重复、保留类型或与已有 `(entity_type, entity_id)` 冲突均保持 Document 调用前状态。稳定错误为 `validation_failed`，`context.reason` 区分 `read_only_source_*_invalid` 与 `read_only_source_identity_conflict`。Document type schema 优先，source schema 只在该 type 未声明时兜底，但 source schema 自身无论是否生效都必须合法。bootstrap 多 source 刷新受 Document mutation session 保护，但 loader 与 AssetStore 的外部副作用不属于这次内存事务，必须可幂等重试。
- **Document 值图只能通过显式变更路径改变**：`schema/config/acl/harvest_rules` 构造时共用 strict JSON 所有权边界（exact builtin、finite number、UTF-8、无循环、深度≤120、整数≤4300位、别名展开后≤250,000 节点），并递归断开输入别名；对应 getter、`meta_dict` 与 `set_acl` 旧值返回深层脱离图。mutation snapshot 只对 JSON 状态做无 hook 复制，loader/validator 作为显式 executable port 保留 identity；未分类扩展对象 fail closed，不执行 `__deepcopy__`。
- **节点边界不是总容量或字节合同**：250,000 约束 strict JSON 单值和单 Entity 完整 durable envelope 的展开节点；不覆盖整个 Document/跨 Entity 总量，也不限制 UTF-8 bytes。snapshot capture 的 8/16 MiB 行级上限是 persistence 层独立 preflight。
- **进入 Document 后 validator authority 唯一**：`add` 与内部 restore/bootstrap 只接受 exact Entity，拒绝子类；无论 standalone Entity 是否 custom，都按 Document 对应 type schema 覆盖。public add 对非空 schema 立即校验完整 data，内部恢复只 attach、不重验历史数据；空/缺 schema 固定 exact no-op。后续 update 校验完整候选 data 一次，不能以 patch 子集或 per-call custom/no-op 绕过 root required、additionalProperties、组合约束及已安装 authority。read-only source 在 Document 未声明该 type 时使用 source schema 的既有兜底规则，仍由 Document 物化路径选择并安装，不由 Entity 自主保留 custom。
- **历史重放有独立私有 seam**：undo/redo 默认可以忠实恢复当前 schema 之前的旧值，但不通过 public `validator=` 降级；replay 仍执行 CAS、strict JSON、完整 Entity envelope 节点预算和 actor log。若某个已经收紧、且下游无法消费旧值的字段显式声明 `x-ze-history-replay-guard: true`，框架会在整组 replay 完成后只按当前字段 schema 重验这些标记字段的最终值，失败则原子恢复整组；同一实体里未标记的历史脏字段和完全未声明的类型继续保留旧值兼容语义。
- **列表顺序是可观察业务语义**：同 type 的 canonical Entity 保持插入顺序，read-only overlay 永远位于 canonical 段之后并保持物化顺序。先注册源、后 `add` 不得导致交错；`Document.save` 的 collections index 必须按 canonical 当前顺序写入，`open` 对所有 legacy 行也走同一插入规则，把混排索引归一为 canonical + overlay，禁止按 id 隐式排序；下一次 save 仅写 canonical 并清理旧只读文件。业务显式 `order` 字段仍是 UI 排序主权威，插入顺序是其稳定兜底。
- **刷新失败事实不能被回滚错误覆盖**：正常 rollback 后原 loader/schema 异常原样重抛；若 rollback 也失败，顶层固定为 `mutation_rollback_failed`，context 同时携带原错误与回滚错误的 type/code，原错误保留为 direct cause，便于 API/日志对账。
- **`collaboration_mode` 必显式声明**——`live_collab` / `with_save` / `realtime_checkpoint` 选一，省略不行（Gate 29）
- **新 mod 显式声明 `save_policy`**——用 `auto_commit` 或 `checkpoint_commit`；`legacy` 只给兼容路径，不存在 manual-save
- **mod 不能直接读写持久化层**——必须走 Document API（AP-Doc-2）
- **所有可能修改内存的提交入口必须先开 mutation session**——未 durable 的失败必须 rollback；
  durable/unknown 结果则必须按 receipt 处理，不能盲目恢复再重放
- **snapshot/session 不得跨 Document 使用或二次 finalize**——同 id 不等于同一内存对象
- **canonical history 身份必须原子安装且不可换代重绑**——bootstrap 同时绑定 exact OpLog、UndoController、authority epoch 与 PID；任一叶子安装失败必须撤销中立 registry 边并只回滚本次精确安装。成功安装过的 Document 不能因 retire 或相同 id 新对象而重用旧运行态，reload 必须发布 fresh Document/Context
- **policy 引用必须闭合到显式业务权限根**——所有 command 注册完后由 framework 运行私有 `_validate_mutation_policy_commands`；通用 `entity.*` 不得提升为 authority，注册后修改公开 spec 也不得改变 frozen authority
- **unsealed 不是运行模式**——它只允许未发布 bootstrap/migration 准备；registry 校验成功后单向 seal，seal 后重装 policy 拒绝，受保护 public direct write 必须同时持有 coordinator coverage 与 command/history authority
- **受保护 Operation 必须覆盖真实 delta**——actor/source、nested Operation 身份和顺序、missing/null、dot ancestry/provenance、create/delete position 与完整 Entity envelope 都必须精确可逆
- **只读 overlay 永远不可写**——policy 不能授予只读 Entity 的 mutation 权，也不把它计入 cardinality

## 9. 自动检查项（audit gates）

发版前 / CI 跑：

- **GATE-DOC-07** `safe_entity_file_store_commit_boundary` — hostile UTF-8 identity 只落 hash、
  safe legacy 自动迁移、symlink 不跟随且零 head 切换、post-rename fsync 诚实返回 UNKNOWN 并对齐版本
- **GATE-DOC-08** `multiprocess_durable_head_fence` — 两个真实 spawn worker 先读同一 head 再同时 save，
  必须恰好一个提交、一个在 generation 写入前 `version_conflict`，最终 head 可完整重开且只含胜者
- **Gate 1** `audit_document_contract` — `document.json` 符合 `entity-contracts.json` 的 `document` 段
- **Gate 29** `audit_collaboration_mode_declared` — `collaboration_mode` 显式声明
- **Gate 37** `audit_read_only_source_protection` — 路径真实存在；read_only entity 不被 touches.writes 引用；dispatcher 拒写 read_only entity
- **Gate 61** `audit_broadcast_acl` — channel 订阅前查 `document.acl.read`；不允许 broadcast 含整 document snapshot 跨 ACL 边界
- **Gate 74** `audit_scalability_limits` — entity count 上限（单 server < 10k / 多人共享 < 50k / 多分片 < 500k）；单 entity 字段 ≤ 100 / data ≤ 1MB

## 10. 别这么干（反模式）

- **AP-01** `document.json` 含 entity 本体或 binary → entity 拆到 `entities/`，asset 拆到 `assets/`
- **AP-02** 一个 skill 实例开多 Document → 不支持；要跨项目走 cross-skill-call
- **AP-03** Document 无 `schema_version` → schema 改了无法迁移
- **AP-Doc-2** 跳过 Document 直接读写持久化层 → 必须走 Document API
- **AP-125** entity 的 `_acl` 不继承 document 的 acl → 默认全公开是安全风险
- **AP-146** 单 Document 装 10 万 entity → 启动慢 + 内存爆（见 Gate 74）
- **AP-Doc-3** handler 抛错或 store 失败后只返回错误、不恢复内存 → 下一次 save 会把半提交写盘
- **AP-Doc-4** rollback 用 deepcopy Entity 替换原对象 → editor/cache 的外部引用继续指向脏对象；
  必须原地恢复原 Entity `__dict__`
- **AP-Doc-5** 把 full snapshot 当长期历史或跨请求缓存 → O(文档总量) 内存膨胀；session 只活在
  一次 commit attempt 内，历史恢复走版本/journal
- **AP-Doc-6** 从 `doc.acl` 取得浅拷贝后原地 append collaborators → 绕过 command、Document CAS、
  OpLog、持久化与 SSE；ACL getter 必须返回完整深拷贝，所有变更只走 `document.set_acl`
- **AP-Doc-7** `entities / entity.type / (entity.id + ".json")` 直接拼路径 → identity traversal、
  alias 与跨平台碰撞；只能消费 `entity_file_store` 的 v2 framed digest
- **AP-Doc-8** 为省事用 `Path.mkdir(parents=True)` 递归造项目祖先 → 在未验证路径上扩张写权限；
  必须从最近一个受信现存祖先 dirfd 逐组件 mkdirat/openat/fstat/fsync

## 11. 当前实装状态

本节区分 Document 已承担的稳定责任，以及明确属于 persistence/transaction 层的后续能力。

### ✅ 已实装

| 项 | 说明 |
|---|---|
| 构造 + 必填校验 | `schema_version`（非空字符串）+ `collaboration_mode`；可选 `save_policy` 只接受 `auto_commit` / `checkpoint_commit` / `legacy` |
| 元属性 property | id / name / schema_version / collaboration_mode / save_policy / effective_save_policy / auto_commit_enabled / should_auto_commit / schema / config / acl / version / created_at / updated_at；所有嵌套 JSON getter 返回深层脱离图 |
| acl 字段 | document 级 owner + collaborators ACL；存在时严格校验形状，坏 durable ACL fail closed；getter 返回深拷贝，变更必须经 `document.set_acl` + Document version CAS；落 `document.json` round-trip |
| `_v` 文档版本号 | 初始 1，每次 save 自动 +1；落 `document.json` round-trip |
| `save(path, expected_version=None)` | 全程 EX flock；锁内 durable `_v/id` CAS；v2 framed identity hash + document-version generation；随机 `O_EXCL` temp、file/dir fsync，最后 `document.json` rename 为唯一 commit point；commit 后锁内清旧 generation/legacy |
| `Document.open(path)` | 全程 SH flock；同一 held root dirfd 内严格解析 document 与完整索引 generation，payload type/id 必须 exact；兼容 strict UTF-8 单组件 legacy `0644/0755` raw tree，并归一 canonical/overlay 顺序 |
| strict legacy scanner seam | 私有 exact capability + topology root `(dev,ino)` 在 I/O 前 fail closed；一个 store/root fd/SH 覆盖 exact head、v2/raw Entity、exact Document 与 yield 内 snapshot capture；Entity 预检 1,000,000、raw 聚合 1 GiB 且剩余预算下推到分配前 file stat，普通 open 兼容合同不变 |
| 存储错误真值 | module-sealed outcome 精确区分 pre-commit NOT_DURABLE 与 post-rename UNKNOWN；前者 Coordinator 可 rollback 且不写 OpLog/publish，后者内存 `_v` 对齐 visible head且绝不 rollback；任意普通 ZeError 仍为 UNKNOWN |
| `register_read_only_source(spec)` | 记录 spec + 调 `loader()` 把返回的 dict 灌成 `read_only=True` entity |
| `read_only_sources() -> [str]` | 返回已注册 source 名清单 |
| collection order | `list(type)` 固定 canonical 插入顺序在前、read-only 物化顺序在后；`save/open` 保真 canonical 顺序并归一 legacy 混排 |
| `DocumentRegistry` | mod 端注册收集器：`declare_entity_type` / `set_collaboration_mode` / `set_save_policy` / `declare_read_only_source` / `set_demo_project` / `build_document` |
| Entity 集成方法 | `add` / `get` / `find` / `list` / `types` / `hard_remove` / `detect_cycles` / `cascade_delete` 全部真实装 |
| 单一 validator authority | 只接受 exact Entity；`doc.add` 覆盖 standalone custom 并立即校验非空 Document schema，内部恢复 attach 不重验，空/缺 schema exact no-op；update 对完整候选 data 跑 [04-field-types](../04-field-types/) 校验 |
| mutation snapshot / restore | 全量快照 Document metadata、collection topology 与 Entity `__dict__`；原地恢复对象身份，覆盖新增与 hard-remove；loader/validator 可执行 port 保留 identity 且不执行 `__deepcopy__` |
| one-shot mutation session | `begin_mutation_session(exclusive=False)` + active/committed/rolled_back/rollback_failed；普通同线程 LIFO 嵌套安全、跨线程 overlap 与二次 finalize 拒绝；exclusive 拒外层/后来 session 并在 I/O 前禁止 live `Document.save` |
| runtime mutation policy | `DocumentRegistry.declare_entity_type(..., mutation_policy=...)` + detached `Document.mutation_policies` + 完整 registry validate 后单向 seal + 受保护 CRUD 的 coordinator/command/history hard fence + 真实 graph delta 授权 + exact forward/reverse coverage + actor/source/ancestry/provenance/position 审计 + 最终 canonical cardinality；未声明类型保持兼容 |

### ⏳ 待补（真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| checkpoint 工作副本 + 崩溃恢复 | `02-data-flow` 启动后按 `checkpoint_commit` 策略实装 |
| 增量 before-state journal | 统一 mutation 写入口 + Entity before-mutation hook 后替换全量 checkpoint；公开 session API 保持不变 |
| `entity-contracts.json` 数据契约锁 | 全部 mod 注册完成 |

### 原则

- **SPEC 是目标态**（§3 物理布局 / §7 完整 mod 注册 API），代码按当前能力实装。
- **公开事务 API 保持稳定**：未来把 full snapshot 换成增量 journal 时，不改变
  `begin_mutation_session()` / `commit()` / `rollback()` 调用方。
- **每补一项更新本表 + §6 标记**：✅ 加进上表；如果发现新依赖再加进下表。
