# 03-asset — Asset

> 一句话：**大文件内容寻址存储**。图片 / 视频 / PDF / 任何 binary 不塞 entity.data，独立按 sha256 落盘。

## 在框架里的位置

- 上一级：[01-data-structure/](../) — 数据结构 5 个叶子
- 同级：[01-document/](../01-document/) / [02-entity/](../02-entity/) / [04-field-types/](../04-field-types/) / [05-capacity-limit/](../05-capacity-limit/)
- mod 配套：**无**（asset 是 framework 内部管理，业务方不需要"声明 asset 类型清单"；通过 `AssetRef` 引用即可）

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览）|
| [SPEC.md](./SPEC.md) | 11 节完整规则（什么 / 内容寻址 / 物理布局 / AssetRef / API / 引用计数 / Document 集成 / Entity 集成 / 持久化 / 红线反模式）|
| [asset.py](./asset.py) | Python 实现（AssetRef + AssetStore + scan_entity_refs）|
| [test_asset.py](./test_asset.py) | pytest 测试（内容寻址、可达性、对账、fail-closed GC、共享池）|

## 当前状态

**✅ 已实装**（全部无跨叶子依赖项）

- `AssetRef` 值类型（frozen dataclass：sha256 / mime / size / ext）+ `to_dict` / `from_dict`
- `AssetStore` 内容寻址存储：
  - `put(bytes, *, mime, ext)` — sha256 dedup + atomic 写
  - `put_file(path)` — 读完整文件 + 自动推断 mime（`mimetypes`）+ ext
  - `get(ref)` — 读 blob + checksum 防篡改（抛 `asset_corrupted`）
  - `has(ref)` / `ref_count(ref)` / `all_refs()`
  - `add_ref(ref)` / `release(ref)` — legacy 派生观测兼容 API；不再授权 GC
  - `scan_document_ref_counts(doc)` — 扫完整 Document（含软删与 read_only）得到 sha 多重集
  - `reconcile_document_refs(doc)` — 用完整可达性重建本地 `meta.refs` 观测
  - `gc(doc)` — 必须持有 canonical Document；先全量对账，再只清不可达本地 blob
- `AssetStore.scan_entity_refs(entity)` static helper — 递归扫 entity.data 找 AssetRef
- 物理布局：`<doc_dir>/assets/blobs/<sha[:2]>/<sha><ext>` + `meta/<sha>.json`
- 浏览器私有交付：页面先用正常 Bearer/cookie 批量换取约 2 分钟的媒体票据；原生
  `<img>/<video>` URL 只带绑定 principal、tool、project、generation 与 sha 的票据，绝不带
  主 session token。GET 会再次读取 durable ACL、generation 与共享资产引用，所有响应
  `private, no-store`；无票据的直接 GET 仍兼容 Authorization/cookie。

**⏳ 待补**（真有跨叶子依赖）

- `ChunkedUploader`（分片上传）— 等 [12-file-search/](../../../) 叶子或 commands-operations
- "asset_ref" 字段类型识别 — 等 [04-field-types/](../04-field-types/)
- `total_size()` 严格汇总本项目本地唯一 blob；每条 meta 的 sha 文件名、ext、声明 size 与非 symlink 本地普通文件 `lstat` 必须一致。HTTP 写入口已消费 [05-capacity-limit/](../05-capacity-limit/) 的单次上传与项目总额双限，任何账实不符都 fail closed，不按零容量放行
- 多进程锁、数据库事务 participant、orphan TTL sweeper — W06 持久化层

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态)。

## 怎么用

```python
from asset import AssetStore
from entity import Entity
from document import Document

# 1. 起 store
store = AssetStore("/path/to/doc/assets")

# 2. 在上传/导入边界（producer 外）读取文件并写 binary
ref = store.put_file("/local/scene.png")
# ref.sha256 == "abc...def"
# ref.mime == "image/png"  (mimetypes 自动推断)
# ref.ext == ".png"
# ref.size == <真实字节数>

# 3. 挂到 entity
e = Entity(type="object", data={"image": ref.to_dict()})

# 4. 挂入 Document。command producer 只改 Document，不写 asset meta
doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
doc.add(e)

# 5. 提交/维护边界按完整 Document 对账；同 sha 多槽位会精确计数
store.reconcile_document_refs(doc)

# 6. GC 必须带 canonical Document；内部会再次全量对账后清 orphan
removed = store.gc(doc)
# removed == [ref.sha256, ...]
```

## 设计要点

- **dedup**：同字节 sha256 一致 → blob 磁盘只一份
- **checksum**：`get` 时重算 sha256 防篡改 / 磁盘损坏
- **唯一回收权威**：完整 canonical Document 可达性；`meta.refs` 只是可重建观测
- **不在 producer 写引用账**：Document 回滚、undo/redo、import/duplicate 都由下一次全量对账收敛
- **release idempotent at 0**：避免双重 release 导致负数 / 崩溃
- **GC fail-closed**：没有 Document 或扫描失败时不删除任何文件
- **producer 唯一 prepare 例外**：`put(bytes)` 可落不可变内容；失败最多留下可去重 orphan，W06 用 TTL sweeper 清理。`put_file` / promote / refs / reconcile / GC 都被运行时 phase guard 拒绝
- **媒体能力最小化**：登录凭据只用于换票，媒体 URL 只获得单项目代际、单 sha、短时读取权；
  ACL 撤销、项目重建或 durable 引用移除会在实际 GET 时立即拒绝

## 跑测试

```bash
cd 01-core/01-data-structure/03-asset/
python -m pytest test_asset.py -v
```
