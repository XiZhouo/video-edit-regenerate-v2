# 02-entity — Entity

> 一句话：**项目里可独立选中的数据原子**。每张图、每个物体、每条记录都是一个 Entity。

## 在框架里的位置

- 上一级：[01-data-structure/](../) — 数据结构 5 个叶子
- 同级：[01-document/](../01-document/) / [03-asset/](../03-asset/) / [04-field-types/](../04-field-types/) / [05-capacity-limit/](../05-capacity-limit/)
- mod 配套：[`../../../02-mod/02-entities.md`](../../../02-mod/02-entities.md) — 业务方在那声明本 skill 的 entity_type 清单 + schema

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览）|
| [SPEC.md](./SPEC.md) | 11 节完整规则（什么是 entity / CAS / 值类型 / schema 校验 / Python API / 软删硬删 / 持久化 / mod 注册 / 红线反模式）|
| [entity.py](./entity.py) | Python 实现 |
| [test_entity.py](./test_entity.py) | pytest 测试 |

## 当前状态

**✅ 已实装**（全部无跨叶子依赖项）

- `Entity` 类：id / type / name / version / data + 8 个 property
- CAS 协议：`update` / `delete` / `restore` 必传 `expected_version`，不匹配抛 `cas_version_mismatch`
- 软删 / 恢复（CAS）；软删后 update 拒，可 restore
- read_only entity：任何写操作抛 `read_only_violation`
- `Actor` 值类型（`user_id` / `device_id` / `source ∈ {user, ai, sync, system}`）
- `AiProvenance` 值类型（model / cost / tokens / trace_id / prompt_template）
- strict JSON 值所有权：所有入站业务树深拷贝归 Entity 所有，所有读 API 返回深层 detached copy
- JSON 边界只接受 exact builtin（有限 float、≤4300 位 int、合法 UTF-8、exact-str dict key），拒绝环、超过 120 层容器或别名展开后超过 250,000 个 value/key 节点的图；字符串逐 codepoint 拒绝 surrogate，不先复制整串 UTF-8 bytes
- 每次成功构造、恢复、update/delete/restore 都先用 `to_dict` 的同一完整 envelope 检查单 Entity 聚合节点预算（data + 全部 provenance + actor_log + metadata），失败不发布半次变更，因此成功返回后 `to_dict()` 必可执行
- 显式 `id` 必须是非空 UTF-8 string，`read_only` 必须是 exact bool；非 `None` validator 必须 callable 且保留 identity
- Validator 协议注入（standalone Entity 可用 custom；进入 Document 后由 Document type schema 唯一接管，空/缺 schema 固定 exact no-op）；每次 update 对完整候选 data 校验一次，兼容参数 `validator=` 只允许省略或重复当前对象，不能按调用替换 authority
- `to_dict` / `Entity.from_dict` 完整 round-trip（version / actor_log / provenance / timestamps 全保真）
- 读取助手：`get(key, default)` / `provenance(field)` / `actor_log()`

这里的 250,000 是**单值/单 Entity 展开节点域**，不是 UTF-8 byte 上限，也不是整个
Document 或跨 Entity 总配额。业务快照 capture 的 8/16 MiB 行级门限是 persistence 层的
另一道独立 preflight，不能由本叶子的节点检查推导。

**⏳ 待补**（真有跨叶子依赖）

- ✅ 真 schema 校验器 — [04-field-types/](../04-field-types/) 已实装；Document.add(entity) 无条件按 entity.type 重装 framework validator（空/缺 schema 重装 exact no-op）并立即校验非空 schema
- ✅ `asset_ref` / `entity_ref` 字段类型 — [04-field-types/](../04-field-types/) 支持
- ✅ `Operation` 共享 Actor — [02-operation/](../../03-commands-operations/02-operation/) 复用 Actor.to_dict()
- ⏳ computed / crdt 字段 — 等 04-history-rollback / 03-offline-first

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态)。

## 怎么用

```python
from entity import Entity, Actor, AiProvenance

# 构造
e = Entity(
    type="object",
    data={"name": "x", "bbox": [0, 0, 100, 100]},
    schema={"name": {"type": "string", "required": True}},
)

# CAS update
actor = Actor(user_id="u1", source="user")
e.update({"name": "x2"}, expected_version=1, actor=actor)
# 现在 e.version == 2

# AI provenance
ai_actor = Actor(user_id="u1", source="ai")
prov = AiProvenance(actor="u1", model="gpt-4", cost_usd=0.01, token_in=100)
e.update(
    {"description": "AI 生成的描述"},
    expected_version=2,
    actor=ai_actor,
    ai_provenance={"description": prov},
)
e.provenance("description")  # → AiProvenance(model="gpt-4", ...)

# 返回值全是深层 detached copy；改它不会反向修改 Entity
view = e.data
view["bbox"][0] = 999
assert e.data["bbox"][0] == 0

# 软删 + 恢复
e.delete(expected_version=3, actor=actor)
e.restore(expected_version=4, actor=actor)

# read_only 拒任何写
ro = Entity(type="config", data={}, read_only=True)
ro.update({"x": 1}, expected_version=1, actor=actor)  # 抛 ZeError("read_only_violation")

# 持久化 round-trip
d = e.to_dict()
restored = Entity.from_dict(d)
assert restored.version == e.version
assert restored.actor_log() == e.actor_log()
```

## 跟 Document 怎么配合

Document 持有 entity-collections（`type → {id → Entity}`）：

```python
from document import Document
from entity import Entity, Actor

doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
doc.add(Entity(type="object", data={"name": "x"}, id="e1"))
doc.get("object", "e1")
doc.list("object")
doc.cascade_delete("object", "e1", actor=Actor(user_id="u1"))
doc.save("project/document.json")  # 拆 entities/object/e1.json
```

## 跑测试

```bash
cd 01-core/01-data-structure/02-entity/
python -m pytest test_entity.py -v
```
