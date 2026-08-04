# 01-document — Document

> 一句话：**skill 的数据根**。一个工具打开后，所有用户能编辑 / 保存的东西都挂在一个 Document 上。

## 在框架里的位置

- 上一级：[01-data-structure/](../) — 数据结构 5 个叶子
- 同级：[02-entity/](../02-entity/) / [03-asset/](../03-asset/) / [04-field-types/](../04-field-types/) / [05-capacity-limit/](../05-capacity-limit/)
- mod 配套：[`../../../02-mod/01-document.md`](../../../02-mod/01-document.md) — 业务方在那声明本 skill 的 Document 长啥样

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览）|
| [SPEC.md](./SPEC.md) | 11 节完整规则（什么是 Document / 数据目录 / 物理布局 / 生命周期 / 字段 / Python API / mod 注册 / 红线 / 自动检查 / 反模式 / 里程碑）|
| [document.py](./document.py) | Document 数据根、持久化门面与兼容导出；组合下列 mutation-policy 叶子，不反向拥有 Command / Operation |
| [document_mutation_session.py](./document_mutation_session.py) | mutation session、提交 candidate、history replay scope、coverage ledger 与 one-shot rollback 生命周期 |
| [mutation_policy_declaration.py](./mutation_policy_declaration.py) | 纯声明所有权：把 schema + passive JSON policy 解析成闭集、detached 的运行时声明；不依赖 Document / Command / Operation |
| [mutation_policy_graph.py](./mutation_policy_graph.py) | 真实 Document+Entity 图的 snapshot、fingerprint、delta、cardinality 与授权 mixin；不向上 import Command / Operation / persistence |
| [entity_file_store.py](./entity_file_store.py) | POSIX 安全文件边界：identity hash、generation、legacy 迁移与唯一 commit point |
| [test_document.py](./test_document.py) | pytest 测试（含持久化失败后的完整内存回滚）|
| [test_entity_file_store.py](./test_entity_file_store.py) | 路径、权限、hardlink/symlink、fsync 与固定 hash 向量对抗测试 |

## 当前状态

**✅ 已实装**（全部无跨叶子依赖项已做完）

- 构造 + 必填校验（`schema_version` / `collaboration_mode`）+ 可选保存策略 `save_policy`
- 全部元属性（含 `acl` / `version`）+ `_v` 文档版本号（round-trip）；`schema/config/acl/harvest_rules` 构造时只接受严格 JSON 并递归断开输入别名，getter/meta 输出也是深层脱离副本，不能绕过命令/CAS 原地改内部状态
- `save(path, expected_version=None)` — 全程持有项目级跨进程独占锁；锁内重读并严格校验 durable head `_v/id`，stale writer 在触碰同名 generation 前固定 `version_conflict`。entity identity 经带域、长度分帧的 SHA-256 映射到 `entities/.ze-store-v2/<document_version>/<shard>/<digest>.json`；先耐久写完整 generation，最后一次 `document.json` rename 是唯一权威 commit point，再在锁内清不可达 generation/legacy 文件。文档版本与显式 CAS 值只接受 exact `int` 的 `1..2^63-1`，上限时原子拒绝继续增长
- `Document.open(path)` — 全程持有同一项目级共享锁并在同一受信 root dirfd 内安全读取 v2 generation，因而 cleanup 不会删除读者已选中的旧 generation；兼容读取无 marker 的旧 `entities/<type>/<id>.json`。legacy identity 接受严格 UTF-8、非空且不含 `/`/NUL 的单 POSIX 组件（拒 `.`/`..`，type 保留 `.ze-store-v2`），包括 Unicode、空格、文件系统可承载的长名称与不冲突的 `.ze-*` id；文件仍须 owner、非 group/world writable、无 symlink/hardlink/special file，下一次成功保存自动迁入 v2 并收紧为 `0600/0700`
- 统一安全 head/metadata 边界：`Document.open`、durable save CAS、`read_document_metadata` 与 topology head context 全部复用同一个 strict parser；duplicate key、NaN/Infinity、超限、坏 id/schema/mode/policy/timestamp/ACL/config/storage marker/collections/source declaration 得到同一 fail-closed 结论，其中无 v2 marker 的 legacy collections 会在零 Entity I/O 的 head 阶段复用同一 POSIX type/id component 与保留目录校验。metadata 读取全程 SH 且零 Entity I/O；完整 exact head bytes 只由 `DocumentHead` 持有，`DocumentMetadata` 在首次严格解析时把 `data_schema/config/acl/collections/entity_storage/read_only_sources` 分别固化为 immutable compact fragment bytes，getter 只解码自己的片段并每次返回 detached graph。因此读取小 ACL/config 不会反复扫描无关的 64 MiB head，调用方也不能借返回值别名改授权、配置或索引。兼容旧 head 缺 timestamps 时不注入当前时间：两者都缺固定为 `0.0/0.0`，只缺一个则继承现存另一个，保证同一 bytes 多次读取结果稳定
  - `open_document_head(path, exclusive=False, missing_ok=False)` 在 context 退出前持续持有 SH/EX，并交付锁内读取的 exact `payload` 与 detached `envelope`；duplicate 可把 verify → safe copy → publish 放进 SH，legacy root migration 用 `exclusive=True, missing_ok=True` 把读取 → copy → publish → source cleanup 放进 EX。后一模式在拿锁后发现 source 已被先行进程清理时返回 `None`，是并发启动的成功 no-op。topology 内部还可传已固定 live root 的私有 `(st_dev, st_ino)`，store 会在自己的 held root fd 上复核，拒绝证明后路径被换成另一个同样“看似安全”的目录
  - strict legacy scanner 另有不导出的 `_open_document_for_legacy_scan`：只有 exact module capability 与 exact root identity 都在任何 store/I/O 前通过，才会用一个 `EntityFileStore`、一个 root fd 和一把 SH 锁读取 exact head、加载 v2/legacy Entity 并 yield `(DocumentHead, exact Document)`；调用方的 snapshot capture 必须留在该 context 内，退出后锁与 fd 自动关闭。scanner 在读第一行前预检最多 1,000,000 个 Entity，并对 raw Entity bytes 设 1 GiB 聚合门限；每行读取把剩余预算下推到 file `fstat`，超限文件在分配 payload 前拒绝，稳定 reason 为 `legacy_scan_entity_count_exceeded|legacy_scan_raw_entity_bytes_exceeded`。普通 `Document.open` 的兼容上限不变。该 capability 只防框架误接线，不是同进程恶意反射沙箱
  - metadata-only rewrite 不是 live mutation API。它是同一受信进程内、由静态 gate 约束唯一消费者的 topology staging primitive：wrapper 先证明 unpublished staging path + prepared duplicate journal，再传真 exact capability，只允许改 `id/name/acl/config`，保留 `_v`、timestamps、collections、storage marker 与未知字段；目标全程 EX、expected-version CAS、atomic rename + fsync。这个 capability 是防误用的框架边界，不声称抵抗能任意 import/反射模块私有对象的同进程恶意代码。pre-rename 失败证明 `NOT_DURABLE`，post-rename fsync 失败证明 `UNKNOWN` 且绝不伪报成功；live metadata 仍必须走 Command → Coordinator → OpLog → Document.save
- `register_read_only_source` — 调 `loader()` 灌 read_only entity
  - 这些实体只属于会话 overlay：可 `get/list`，但 `save()` 永不写进 canonical `collections` / `entities/`；旧版本误落盘的只读行会在下一次保存时清理
  - 单次注册先把 source schema 与每行 exact JSON 深度隔离并完整校验、后整批发布；只接受有限数字、合法 UTF-8、无循环、业务值深度不超过 120 且别名展开后不超过 250,000 节点的内建 JSON 值，`id`/`name`/`data` 分别必须是非空字符串（或缺失）、字符串、对象。loader 可复用嵌套缓冲，但中途失败、非法值、批内重复或与 canonical 同 `(type,id)` 冲突时，source 与实体都保持调用前状态
  - Document 的 type schema 是唯一权威；只有 Document 未声明该 type 时才以 source schema 兜底。刷新器跨多个 source 失败时会恢复整个 Document 内存态；loader/AssetStore 外部副作用须自行幂等
  - `Document.list(type)` 的顺序固定为“canonical 插入顺序 + read-only 物化顺序”；先物化源、后新增本地实体也不会交错。`save/open` 保留 canonical 顺序，不再按 id 偷偷重排；legacy 混排索引打开后也会归一，下一次 save 清除只读旧行
- 8 个 entity 集成方法：`add` / `get` / `find` / `list` / `types` / `hard_remove` / `detect_cycles` / `cascade_delete`
- 完整 mutation checkpoint：`capture_mutation_snapshot` / `restore_mutation_snapshot` / `begin_mutation_session`
  - 回滚保持原 Document 与原 Entity 对象身份
  - 精确恢复 entity data/version/deleted/provenance/actor_log/validator/timestamps
  - source loader 与 Entity validator 是可执行 port：快照/恢复保持原对象身份，绝不调用其 `__deepcopy__`
  - 自动移除会话新增 entity，恢复会话中 hard-remove 的原对象
  - session 的 `commit()` / `rollback()` 都是 one-shot，不能二次 finalize
  - runtime bootstrap 使用 `begin_mutation_session(exclusive=True)`：拒绝外层/后来/cross-thread session，且 active 期间 `Document.save` 在任何文件 I/O 前失败；普通命令 session 仍可同线程 LIFO 嵌套
- canonical history runtime 身份只由 bootstrap 私有协议安装：exact OpLog、UndoController、authority epoch 与 issuer PID 必须一次性共同落到同一个 Document；若 controller 或 Document 安装中断，registry 边与已安装叶子按精确 epoch/PID 回滚。安装成功后同一 Document 即使 authority retire 也不能原位换代重绑，reload 必须创建 fresh Context/Document，防止旧对象悄悄接到新历史上
- `DocumentRegistry` 完整流转 — `declare → build_document → demo bootstrap → save → open`
- 可选类型级 runtime mutation policy（默认关闭、声明后闭集）：按最外层业务命令授权 create/rename/soft-delete/restore/hard-remove 与每个顶层字段；框架比较真实 Document 前后差异，而不是相信 handler 的文字声明或返回的 command id。可选 `cardinality.max_total` 在 canonical 最终态强制（软删仍计数，read-only overlay 不计数）
  - policy 引用的每个命令都必须真实注册，且注册时显式 `mutation_authority=True`；通用 `entity.*` helper 默认且固定不是权限根，只能被业务根在同一 registry、Document、actor/source 下嵌套消费
  - 生命周期是单向的：runtime declaration 先在未发布的 bootstrap/migration 准备态安装，framework + mod commands 全注册后由 framework bootstrap 运行私有 `_validate_mutation_policy_commands` 并 seal；mod/downstream 不直调这项 authority，seal 后也不能原位重装 policy。此后受保护类型的 `Entity.update/delete/restore` 与 `Document.add/hard_remove` 必须同时处于 CommitCoordinator coverage 和真实业务 command authority 下，history replay 则必须处于 coordinator-owned replay coverage；公开请求里的直接写在变化前 fail closed
  - create 是组合授权：除 `lifecycle.create` 外，非空 `Entity.name` 还要 `rename`，初始 `data` 的每个顶层字段还要对应 field 权限；只读 overlay 永远不可写，也不能靠 policy 授权
  - policy 是部署/runtime 权威，不写 `document.json` 或 business snapshot；bootstrap/runtime rehydration 每次从当前代码声明重装，policy-only 更新不 bump 业务版本
  - 受保护 graph delta 还必须与 forward/reverse Operation patches 的身份、顺序和值精确可逆；dot-path 按顶层根字段授权，但 Patch/provenance 保留原始路径，缺失/null/标量父链由 `ancestry` 精确还原，结构创建/物理删除由 `position` 保持 canonical 顺序
  - 只有真实受保护变化或声称触碰受保护类型的 patch 才进入严格路径；同一 Document 中未声明 policy 的类型继续走旧兼容语义。幂等 delete/restore 仍先验 CAS/read-only，成功 no-op 返回空 patch 且不 bump Entity version
  - schema/policy/collection/bucket/entity authority、严格 durable scalar、类型敏感 JSON 和 canonical-before-overlay 顺序均在命令回滚边界内校验；任何失败在存储调用前恢复
- ✅ 单一 validator authority — standalone Entity 可 custom；只接受 exact Entity 进入 Document，`doc.add(entity)` 会无条件按 type 重装 Document framework validator（非空 schema 立即校验，空/缺 schema exact no-op），restore/bootstrap 则 attach 而不重验历史数据；后续 update 对完整候选 data 校验一次，per-call `validator=` 不能换成 custom/no-op 绕过。历史重放默认兼容旧值；收紧后不可再消费旧值的字段可声明 `x-ze-history-replay-guard: true`，让整组 undo/redo 在提交前按当前 schema 重验并原子回滚非法复活

250,000 是 strict JSON 单值以及单 Entity durable envelope 的展开节点边界，不是整个
Document、跨 Entity 或 UTF-8 byte 配额；snapshot capture 的 8/16 MiB 行级门限属于
persistence 的独立 preflight，不能互相替代。

文件边界支持 Python 3.9+ 的 macOS/Linux POSIX API，不依赖 Linux-only syscall。缺失的项目
父目录从最近一个 owner/mode 安全的现存祖先 fd 起，逐组件 `mkdirat(0700) → openat + fstat →
fsync`，保留旧版递归建目录能力但不退回 `Path.mkdir(parents=True)`；中间 symlink 或非 sticky
的共享 group/world-writable 祖先直接拒绝；标准 root-owned sticky `/tmp` 可作为显式 trust anchor。
项目根、既有 legacy `0644` 文件/`0755` 目录可兼容读取，
但任何 group/world writable、wrong-owner、symlink、hardlink 或特殊文件都 fail closed。错误只给
稳定 code/reason，不携带绝对路径或业务 identity。同 UID 恶意进程并发替换更高祖先不在此
边界的沙箱承诺内。`.ze-document.lock` 的有界 POSIX `flock` 负责同一项目内 head/generation
读写互斥；项目 replace/delete 等生命周期 namespace fencing 仍由 ProjectTopology 拥有。

**⏳ 待补**（真有跨叶子依赖）

- `.recovery/` 工作副本与崩溃恢复 — 跟 `checkpoint_commit` 和 durable journal 绑定
- full snapshot 的增量替代 — 等所有 mutation 统一写入口并具备 Entity before-mutation hook
- SQLite/Postgres、OpLog 与 outbox 的物理事务 — 由 `02-data-flow/07-persistence` 拥有，
  不塞回 Document

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态)。

## 怎么用

```python
from document import Document, DocumentRegistry, ReadOnlySourceSpec
from entity import Entity, Actor

# 方式 A：直接构造
doc = Document(
    schema_version="1.0.0",
    collaboration_mode="live_collab",
    save_policy="auto_commit",       # 新工具显式声明；旧文档缺省时安全映射为 auto_commit
    name="my-project",
    schema={"object": {"name": {"type": "string"}}},
    acl={"owner": "alice", "collaborators": ["bob"]},
)
doc.add(Entity(type="object", data={"name": "样例物体"}))
doc.save("project/document.json", expected_version=1)  # 409 check
doc2 = Document.open("project/document.json")
assert len(doc2.list("object")) == 1

# framework metadata/topology consumer：不加载 Entity，返回深层 detached 快照
from document import open_document_head, read_document_metadata
meta = read_document_metadata("project/document.json")
with open_document_head("project/document.json") as head:
    assert head is not None
    exact_head_bytes = head.payload       # 必须在这个 context 内完成 snapshot copy
    detached_envelope = head.envelope     # 不要重新 Path.read/json.loads
# bootstrap 会把旧项目缺失/legacy 声明幂等升级为 mod 当前 save_policy；
# 直接使用 Document 时也可显式调用 doc2.adopt_save_policy("auto_commit")。

# 方式 B：通过 mod registry（业务方推荐路径）
registry = DocumentRegistry()
registry.declare_entity_type(
    name="object",
    schema={"name": {"type": "string"}},
    mutation_policy={
        "schema": "ze.entity-mutation-policy.v1",
        "lifecycle": {
            "create": [], "rename": [], "soft_delete": [],
            "restore": [], "hard_remove": [],
        },
        "fields": {"name": ["object.update-name"]},
        "cardinality": {"max_total": 1},
    },
)
registry.set_collaboration_mode("live_collab")
registry.set_save_policy("auto_commit")
registry.set_demo_project(name="demo", bootstrap=lambda doc: [
    doc.add(Entity(type="object", data={"name": "样例"}))
])
doc = registry.build_document(schema_version="1.0.0", name="my-project")
registry.demo["bootstrap"](doc)  # 灌入 demo entity

# 3. 先注册通用 helper，再注册显式业务权限根；全部注册完才闭合 policy 引用
from command import CommandMutation, CommandRegistry
from builtin_commands import register_builtin_commands
from operation import new_operation

commands = CommandRegistry()
register_builtin_commands(commands)

@commands.command(
    id="object.update-name",
    label="修改物体名称",
    mutation_authority=True,       # 只给面向业务的根命令；不要给 entity.update
)
def update_name(args, actor, source, doc):
    entity = doc.get("object", args["id"])
    helper = commands.dispatch(
        "entity.update",
        {
            "type": "object",
            "id": entity.id,
            "patches": {"name": args["name"]},
            "expected_version": entity.version,
        },
        actor=actor,
        source=source,
        doc=doc,
    )
    root = new_operation(
        command_id="object.update-name",
        source=source,
        actor=actor.to_dict(),
        patches=(),
        reverse_patches=(),
    )
    return CommandMutation((root, helper))

doc._validate_mutation_policy_commands(commands)  # framework bootstrap only；成功后单向 seal
execution = commit_coordinator.execute(
    lambda: commands.dispatch(
        "object.update-name",
        {"id": doc.list("object")[0].id, "name": "改名后"},
        actor=Actor(user_id="u1"),
        source="user",
        doc=doc,
    )
)
# dispatch 必须发生在 coordinator producer 内；execution.result 是已按 receipt 处理的 mutation。
```

### 框架提交失败时回滚

```python
session = doc.begin_mutation_session()     # 必须在 coordinator 的 mutation lock 内
try:
    operation = execute_command()          # handler 可以就地修改 Document / Entity
    receipt = commit_store.commit(...)
    if not receipt_is_safe_to_keep(receipt):
        session.rollback()                 # 原对象原地恢复；返回 None
    else:
        session.commit()                   # durable 后关闭回滚凭据
except Exception:
    if session.active:
        session.rollback()
    raise
```

这是 W04 的正确性基线：每次 capture 会深拷全部 Document 元数据和全部 Entity 状态，
时间与内存都是 O(文档总状态)。它适合先消灭“handler 改了内存、持久提交失败却留下脏状态”
的问题，但不是大型编辑器的最终性能形态。后续所有 mutation 统一经过可观察写入口后，应在
不改变公开 session API 的前提下，替换为按触达对象记录 before-state 的增量 journal。

## 跑测试

```bash
cd 01-core/01-data-structure/01-document/
python -m pytest test_document.py -v
python -m pytest test_entity_file_store.py -v
```
