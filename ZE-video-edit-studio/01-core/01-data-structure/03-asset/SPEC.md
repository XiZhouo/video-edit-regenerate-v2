# Asset — 大文件内容寻址存储

## 1. 这是什么

Entity 装 JSON 结构化数据（字符串 / 数字 / 列表 / 嵌套对象），但**不装 binary**——图片、视频、PDF、音频、3D 模型等大文件全走 AssetStore。

业务白话视角：业务同事在 ZE-seek-find-object-generator 上传一张场景图，那张 PNG 不会塞进 entity.data，而是经 AssetStore 落到 `<doc_dir>/assets/blobs/`；entity.data 里只放一个 `AssetRef` 指针（sha256 + mime + size + ext）。同一张图被多个 entity 引用，磁盘上只存一份。删完最后一个引用，自动 GC。

### 为什么不塞 entity

| 问题 | 原因 |
|---|---|
| **JSON 膨胀** | 一张 1 MB PNG base64 后 1.3 MB 塞进 entity.json，列 1000 个 entity 就 1.3 GB；entity 读取/序列化/网络传输都拖垮 |
| **无法 dedup** | 同一张图被 100 个 entity 引用，entity.data 存 100 份；磁盘浪费 100 倍 |
| **不能流式** | entity 是整体 JSON 读写；大文件无法分片传 / 部分下载 / 流式预览 |
| **修改成本** | 改 entity 的一个 metadata 字段，整个 entity 重写 — 包括那 1 MB binary 重写 |

## 2. 内容寻址原理

**sha256 寻址**：用 `sha256(bytes)` 作 ID。同内容必然同 sha；不同内容几乎必然不同 sha（碰撞概率天文数字）。

**dedup**：`put(data)` 算 sha 后查 `blobs/<sha[:2]>/<sha>` 是否已存在；存在就跳过（idempotent），返回同一个 `AssetRef`。100 个 entity 引用同图 → 磁盘只一份 blob。

**checksum 防篡改**：`get(ref)` 读 blob 后重新算 sha256，跟 `ref.sha256` 不匹配抛 `asset_corrupted`。磁盘损坏 / 人为篡改 / 备份恢复出错都能被发现。

## 3. 物理布局

```
<doc_dir>/assets/
├── blobs/
│   ├── ab/                            # sha256 前 2 字符分片 (256 个目录)
│   │   ├── abc123...def.png
│   │   └── abc456...fff.jpg
│   └── cd/
│       └── ...
└── meta/
    ├── abc123...def.json              # {"mime":"image/png","size":12345,"refs":2,"ext":".png"}
    └── ...
```

**为什么分片**：单目录 10 万文件会让 `ls` / `iterdir` 卡顿；按 sha256 前 2 字符分 256 个子目录，每子目录均匀分布。

**为什么 meta 单独存**：
- `refs` 是从 Document 可达性重建的派生观测；写 meta 不动 blob，避免大文件重写
- meta 是小文件（< 1 KB），atomic 写代价低
- blob 内容寻址不可变，meta 是 mutable 状态——分离避免概念混淆

**为什么 ext 在文件名**：磁盘上 `ls` 能看出 PNG / MP4，便于人工排查 / 备份工具识别 / OS 缩略图。ext 重复也存在 meta，作 source of truth。

## 4. AssetRef 值类型

```python
@dataclass(frozen=True)
class AssetRef:
    sha256: str                                     # 64 个 hex 字符
    mime: str = "application/octet-stream"
    size: int = 0
    ext: str = ""                                   # 含前导点, e.g. ".png" / ".mp4" / ""

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "AssetRef": ...
```

**为什么 frozen**：AssetRef 是值类型，hash 一致性 + 不可变性给后续做 `set[AssetRef]` / `dict key` 留口子。

**entity.data 里的形态**：

```json
{
  "image": {
    "sha256": "abc123...",
    "mime": "image/png",
    "size": 12345,
    "ext": ".png"
  }
}
```

entity 看到 `data` 里有 dict 含 `sha256` 字段就视为 AssetRef（不需要额外类型标记）。`AssetStore.scan_entity_refs(entity)` 帮 caller 扫出来。

## 5. AssetStore API

```python
from asset import AssetStore, AssetRef

# 构造 (root = <doc_dir>/assets/, 自动 mkdir blobs/ + meta/)
store = AssetStore("/path/to/doc/assets")

# ──────── 写入 ────────
ref = store.put(data: bytes, *, mime: str = "...", ext: str = "") -> AssetRef    # ✅
ref = store.put_file(path: str | os.PathLike, *, mime: str | None = None) -> AssetRef  # ✅
                                                                                  # mime 默认 mimetypes.guess_type 推断
                                                                                  # ext 从 path 后缀推断

# ──────── 读取 ────────
data = store.get(ref: AssetRef) -> bytes                                          # ✅ checksum 防篡改
ok = store.has(ref: AssetRef) -> bool                                              # ✅

# ──────── 可达性 / 派生观测 ────────
counts = AssetStore.scan_document_ref_counts(doc) -> Counter[str]                  # ✅ sha -> 槽位数
local = store.reconcile_document_refs(doc) -> dict[str, int]                       # ✅ 重建本地 meta.refs
count = store.ref_count(ref: AssetRef) -> int                                      # ✅ 只读派生观测
store.add_ref(ref) / store.release(ref)                                             # ✅ legacy 兼容,非 GC 权威

# ──────── GC ────────
removed = store.gc(doc) -> list[str]                                               # ✅ 先全量对账,再清不可达本地项

# ──────── 辅助 ────────
all = store.all_refs() -> list[AssetRef]                                           # ✅ 列全部 (含 refs=0)
refs = AssetStore.scan_entity_refs(entity) -> list[AssetRef]                       # ✅ static, 扫 entity.data
```

## 6. Document 可达性协议

**唯一回收权威**：canonical Document 当前完整终态。`scan_document_ref_counts(doc)` 遍历
所有类型并用 `include_deleted=True` 读取 entity，因此同时覆盖普通新增、批量新增、更新、
导入、复制、undo、redo 与硬删。它不依赖“某条命令是否记得加减计数”。

**为什么是多重集**：同一 sha 可能同时出现在 hero、thumbnail 两个槽位，也可能被多个
entity 共用。扫描按每个槽位计数；集合视图只适合读取权限判断，不能作为诊断账。

**为什么软删与 read_only 仍可达**：软删可 restore；read_only source/overlay 仍是当前
编辑器可见数据。漏掉任一类都可能让 GC 删除下一次恢复或渲染所需的 blob。

**对账**：`reconcile_document_refs(doc)` 先完整扫描 Document、再完整解析本地 meta，确认
两者都可读后才把每个本地 `refs` 改成扫描槽位数。Document 引用的 fallback/shared blob
不会在本地凭空创建 meta。`add_ref/release` 暂留兼容与观测用途，但其历史不授权删除。

**GC fail-closed**：`gc(doc)` 必须收到 canonical Document，先对账，再只删除槽位数为 0
的本地 blob。没有 doc、扫描失败或 meta 损坏时抛错，删除数为 0；legacy `refs=0` 绝不
等同“已证明不可达”。调度仍由 framework 在已提交后的维护窗口负责。

**执行阶段**：producer 中禁止 `put_file`、`promote`、`add_ref/release`、reconcile 与 GC。
唯一例外是 `put(bytes)`：它只准备不可变、内容寻址、可去重的 blob；业务提交失败最多留
下不可达 orphan，不会改变 GC 权威。W06 通过 TTL sweeper 清 orphan，并为 reconcile/GC
补多进程锁与数据库事务 participant。

## 7. 跟 Document 集成

**AssetStore 独立持有 blob，Document 独立持有业务终态**。Document 不持有 AssetStore；
AssetStore 只通过 `types/list(include_deleted=True)` 的最小只读协议计算可达性。

理由：
- **关注点分离**：Document 管 entity-collections，AssetStore 管 blob；生命周期在明确维护边界对账
- **测试简化**：Document 测试不需要造 AssetStore；AssetStore 测试不需要 mock Document
- **mod 灵活性**：业务方可能想用不同 store 后端（本地 / S3 / IPFS）；耦合会卡死

**集成路径**：

```python
from document import Document
from entity import Entity
from asset import AssetStore

store = AssetStore("/path/to/doc/assets")
doc = Document(schema_version="1.0.0", collaboration_mode="with_save")

# 上传/导入边界先把文件转成 bytes；producer 内只允许 put(bytes) 准备不可变内容
img = store.put_file("/local/scene.png")
e = Entity(type="object", data={"image": img.to_dict()})
doc.add(e)

# 已提交后的维护边界统一对账；GC 自己也会先再对账一次
store.reconcile_document_refs(doc)
store.gc(doc)
```

command handler 不得调用 `add_ref/release`。这是刻意的事务边界：W04 能回滚内存
Document，却不能回滚已经写出的 meta；全量对账让所有 mutation 路径用同一套事实收敛。

## 8. 跟 Entity 集成

**Entity 不感知 AssetRef**：entity.data 是普通 dict，里面有 AssetRef serialize 形态的字典（`{"sha256":..., "mime":...}`）就是它。

**为什么不让 Entity 类感知**：
- AssetRef 是 03-asset 的概念，Entity 是 02-entity 的概念；让 Entity 类反向依赖 03-asset 会把"叶子自治"打破
- Entity 关心的是数据合法性（schema 校验）+ CAS，不关心字段是不是指针
- caller 用 `scan_entity_refs(entity)` 扫即可

**04-field-types 启动后**：会加 "asset_ref" 字段类型，schema 里写 `image: {type: asset_ref}`，由 field_types validator 检查值的形态正确（含 sha256 / mime / size / ext）。本叶子不依赖那个。

## 9. 持久化协议

**blob 文件**：
- 路径：`blobs/<sha[:2]>/<sha><ext>`
- 内容：原始字节（一字节不动）
- atomic：先写 `<path>.tmp`，再 `os.replace(tmp, final)`
- 已存在则跳过（dedup）

**meta 文件**：
- 路径：`meta/<sha>.json`
- 格式：
  ```json
  {
    "mime": "image/png",
    "size": 12345,
    "refs": 2,
    "ext": ".png"
  }
  ```
- atomic：`.tmp` → `os.replace`
- `put(data)` 时如果 meta 已存在不覆盖（保留现有 refs），只新建则 refs=0
- `refs` 不是手工维护的生命周期 source of truth；它由 `reconcile_document_refs(doc)`
  从 canonical Document 完整重建，可用于诊断、容量展示与兼容读

**checksum**：
- `get(ref)` 读 blob 后重算 sha256
- 不匹配抛 `ZeError("asset_corrupted", context={"expected":..., "actual":..., "sha256":ref.sha256})`

**gc 顺序**：
- 必须先完整扫描 Document、解析全部本地 meta、完成 refs 对账
- 只把扫描槽位数为 0 的本地 sha 列入删除计划
- 先 `unlink(blob)` 再 `unlink(meta)`
- 中断不会让 meta 指向不存在的 blob（meta 还在意味着 blob 必在；崩在中间下次 gc 清干净）
- 完成后清理空的 `blobs/<sha[:2]>/` 分片目录

### 9.1 浏览器媒体私有交付

浏览器原生 `<img>`、`<video>` 不能可靠附加 `Authorization` header，但这不构成把主 session
token 放进 URL 的理由。标准流程固定为：页面通过普通认证 `POST /api/asset-tickets`，按当前
project generation 批量提交 sha；服务端只为当前 durable 可读资产签发短期票据。票据中的主体
是域分离 HMAC 指纹，不是用户名；签名同时绑定 tool、project、generation、sha 与到期时间。
同一稳定 credential master key 派生媒体签名 key，多进程使用同一部署 key；key 缺失或损坏
必须启动失败，不得退回每进程随机 key。

`GET /api/asset/{sha}` 有两条明确路径：媒体票据路径先验签，再加载项目；随后在生命周期 lease
内重读 durable `document.json` 的 ACL/config/collections 及索引指向的 entity 文件，确认主体仍是
成员、generation 未变、共享池 blob 仍被该项目耐久引用，最后才读字节。普通 Authorization 或
HttpOnly cookie 直接 GET 保持兼容，但旧 `?token=<session>` 资产 URL 必须当匿名请求拒绝。
本地项目 blob 维持项目成员可读语义；共享池 fallback 额外要求 durable 引用，不能信任进程缓存
中的旧 Document。

票据批次把“资产不存在”也视为一个短期权威结果，`tickets` 与 `missing` 必须无交集且恰好覆盖
请求集合；这样缺失资源不会触发客户端热循环。成功、404、401、403 等所有资产响应都必须带
`Cache-Control: private, no-store`（以及 `Pragma: no-cache`、`Vary: Authorization, Cookie`、
`Referrer-Policy: no-referrer`），防止代理或浏览器在撤权后复用旧的字节或授权判断。

## 10. 红线规则 + 反模式

红线（违反破坏一致性 / 资源浪费）：

- **entity.data 不能存 binary** — 必须经 AssetStore（AP-E4，跟 02-entity SPEC §10 一致）
- **不能绕过 AssetStore 直接读写 blob** — checksum / 可达性对账会失效（AP-A1）
- **不能手改 meta.refs** — 它是可重建观测，不是业务输入（AP-A4）
- **不能无 Document 调 GC** — 未知不是不可达；必须 fail-closed（AP-A5）
- **不能在 producer 做 asset 维护写** — Document 回滚无法回滚外部文件（AP-A2）
- **不能基于 `ref.size` 信任** — caller 传的 size 跟实际 bytes 长度可能不一致；store 内部用 `len(data)` 算 size
- **主 session token 不能进入资产 URL** — 原生媒体只用短期 asset ticket；session query 只保留给
  框架的 EventSource 兼容路径
- **不能只在签票时校权** — 实际 GET 必须重新检查 durable ACL、generation 与共享引用；否则撤权
  后票据仍可读、或另一 worker 的旧内存快照会越权

反模式：

- **AP-A1** 业务代码直接 `Path("/.../blobs/abc").write_bytes(...)` → 应走 `store.put(data)`
- **AP-A2** command handler 调 `add_ref/release/reconcile/gc` → producer 后续失败时留下外部漂移；运行时 phase guard 会拒绝
- **AP-A3** 为 add/update/hard_remove/undo 各写一套增减 hook → 路径迟早漏接；统一扫完整 Document
- **AP-A4** 手动改 `meta/<sha>.json` 的 refs 字段 → 观测失真；下一次 reconcile 会覆盖
- **AP-A5** 调 `store.gc()` 不传 Document，或传只列活跃 entity 的局部视图 → 未证明不可达；API 显式拒绝前者，调用方必须保证后者是 canonical
- **AP-A6** 把同一个文件用不同 mime / ext 多次 `put` → blob 只存一份没问题；但 caller 拿到的 AssetRef.mime / ext 是**最后一次 put 时传的值**，可能跟语义不符
- **AP-A7** 跨 Document 共享 AssetStore root → v3 单 Doc 约定下没意义；每个 Doc 自己的 store

## 11. 当前实装状态

### ✅ 已实装

| 项 | 说明 |
|---|---|
| `AssetRef` 值类型 | frozen dataclass: sha256 / mime / size / ext + to_dict / from_dict |
| `AssetStore(root)` 构造 | 自动 mkdir blobs/ + meta/ |
| `put(bytes, *, mime, ext)` | sha256 dedup + atomic + meta 创建（refs=0）|
| `put_file(path, *, mime=None)` | 读完整文件；mime 自动 mimetypes.guess_type；ext 从 path 推 |
| `get(ref)` | 读 blob + checksum 校验；不存在抛 `asset_not_found`；篡改抛 `asset_corrupted` |
| `has(ref)` | blob 文件存在性查 |
| `add_ref(ref) / release(ref) / ref_count(ref)` | legacy 派生观测兼容；release idempotent at 0；不授权 GC |
| `scan_document_ref_counts(doc)` | 全量扫描含软删/read_only 的 sha 多重集 |
| `reconcile_document_refs(doc)` | 先完整读取再以可达性重建本地 meta.refs |
| `gc(doc)` | 无 Document fail-closed；先对账再清不可达本地项 |
| `all_refs()` | 列全部已知 AssetRef（含 refs=0 pending GC）|
| `AssetStore.scan_entity_refs(entity)` static helper | 递归扫 entity.data 里 dict / list 找 sha256 字典 → [AssetRef] |
| producer effect guard | 禁 put_file/promote/refs/reconcile/gc；只允许 put(bytes) immutable prepare |
| 浏览器媒体票据 | 稳定 key、短 TTL、opaque principal/tool/project/generation/sha 绑定；GET 重验 durable 权限，响应 private/no-store |
| ZeError 共享 | 从 `entity` 模块 import（跟 document 一致）|

### ⏳ 待补（真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| ChunkedUploader（分片上传）| `12-file-search` 叶子或 `commands-operations` |
| "asset_ref" 字段类型识别 | `04-field-types` 完成后加该类型 + 真校验器 |
| 单件与单项目本地 blob 总额 | `total_size()` 逐项校验 canonical sha 文件名、安全 ext、声明 size 与非 symlink 普通文件 `lstat` 一致，再由 `05-capacity-limit` 拦截；账实不符 fail closed。跨项目/账号全局配额与租户计费留事务配额账本 |
| 多进程互斥 / DB transaction participant / orphan TTL sweeper | W06 persistence |
| 分布式后端（S3 / IPFS）| 后续工程层，本地存储够用 |

### 原则

- **Document 终态优先**：mutation 路径不维护 GC 权威；完整可达性可重建一切派生账
- **GC 调度时机**：`gc(doc)` 只能在已提交后的维护窗口执行
- **W05/W06 边界**：当前保证单进程阶段正确；多进程原子性与 orphan 长期治理在 W06
