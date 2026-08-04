# Entity — 项目里可独立选中的数据原子

## 1. 这是什么

Document 是数据根，Entity 是单元。每张图、每个物体、每条记录、每个图层、每条审核意见，凡是**可独立选中、独立编辑、独立 ACL** 的"东西"，都是一个 Entity。

业务白话视角：业务同事在 ZE-seek-find-object-generator 里点中的"一个物体"、ZE-knowledge-base 里点中的"一条知识条目"，都是一个 Entity。Entity 有 id / type / version / data / 时间戳 / 软删标记 / 审计日志，所有变更经 CAS 协议保证幂等。

### Entity vs Value Object — 设计指引

| 这是什么 | 怎么放 |
|---|---|
| 能独立选中、独立编辑、单独可删 | **Entity**（自己的 id，进 Document collections）|
| 永远附着在父对象上，离开父对象无意义 | **Value Object**（inline 进父 entity.data）|
| 大量同质数组（瓦片地图、像素数据） | **整数组**包成一个 Entity，不是每元素一 Entity（避免 100k+ entity 启动慢）|
| 外部只读数据（如配置表） | Entity，但 `read_only=True`，dispatcher 拒任何写 |

## 2. 字段定义

Entity 的 12 个内部字段：

| 字段            | 类型                        | 必填  | 说明                                          |
| ------------- | ------------------------- | --- | ------------------------------------------- |
| `_id`         | string                    | ✓   | uuid4 hex 默认；显式值必须是非空 exact UTF-8 string       |
| `_type`       | string                    | ✓   | mod 端 `declare_entity_type` 时的 name         |
| `_name`       | string                    |     | 人看的名字（可改）                                   |
| `_data`       | dict                      | ✓   | 业务数据；schema 校验范围                            |
| `_version`    | int                       | ✓   | CAS 版本号；初始 1，每次成功 update/delete/restore +1  |
| `_deleted`    | bool                      | ✓   | 软删标记；默认 False                               |
| `_read_only`  | bool                      | ✓   | 默认 False；构造参数必须是 exact builtin bool             |
| `_schema`     | dict                      |     | 校验规则；构造时传，跟 validator 配套                    |
| `_provenance` | `dict[str, AiProvenance]` |     | field_name → AI 溯源信息（哪个模型 / 多少钱 / 多少 token） |
| `_actor_log`  | `list[dict]`              |     | 变更审计：`[{version, actor, ts, fields}]`       |
| `_created_at` | float                     | ✓   | 时间戳                                         |
| `_updated_at` | float                     | ✓   | 每次成功变更更新                                    |
|               |                           |     |                                             |

### 2.1 Strict JSON 值所有权

Entity 把业务 JSON 当作**值树**而不是共享对象图。`data` / `schema` / update
`patches` / update `name` / AI provenance 在提交前全部复制归 Entity 所有；输入端后续
修改不能穿透。`data`、命中的 `get()`、`to_dict()`、`from_dict()` 恢复输入、
`provenance()` 与 `actor_log()` 同样做深层 detach，输出端修改也不能穿透。

私有共享边界 `_clone_json_value(value)` 只接受：

- exact builtin `None`、`bool`、`int`、`float`、`str`、`list`、`dict`；拒绝所有子类
- 最多 4300 位十进制数字的 `int`（正负号不计位数）
- finite `float`（拒绝 NaN / ±Infinity）
- 可按 strict UTF-8 编码的 `str`；dict key 必须是 exact builtin UTF-8 `str`；实现逐
  codepoint 拒绝 surrogate，不为检查先分配整串 UTF-8 bytes
- 最多 120 层业务值容器（为外层格式结构预留深度余量）；拒绝 active-path cycle；
  别名展开后整图最多 250,000 个 value/key 节点

共享但无环的输入容器会展开成独立树枝，不保留 alias。实现手写遍历，不用
`deepcopy` / pickle / 序列化 round-trip，也不调用 payload 的 copy、repr、迭代等
用户 hook。所有拒绝统一抛 payload-free
`ZeError("validation_failed", "value must be strict JSON data")`。`04-field-types`
复用同一个私有 helper 持有 framework schema，避免两套边界漂移。

除单个入站值外，Entity 还用 `to_dict()` 的同一个完整 durable envelope helper 对
`data + 全部 provenance + actor_log + metadata` 共享 250,000 节点预算。构造、
`from_dict` 与 update/delete/restore 都在发布任何新状态前预检候选 envelope；因此任何
成功返回的 Entity 都能立即 `to_dict()`，聚合越界失败也不会改变 name/version/data/
provenance/log/timestamps。这个数字是展开节点域，不是 UTF-8 byte 配额，更不是整个
Document 或跨 Entity 总量承诺；persistence snapshot 的行级 8/16 MiB 是独立门限。

## 3. CAS 协议（Compare-And-Swap）

Entity 的**唯一变更协议**。所有变更方法（`update` / `delete` / `restore`）必须传 `expected_version`，跟当前 `_version` 不一致就抛 `ZeError("cas_version_mismatch")`。

版本值域与后续 SQLite authority 一致：当前版本和 `expected_version` 都必须是 exact
`int`，范围 `1..2^63-1`；`bool`、float、int 子类和带自定义 equality hook 的对象都不能
参与比较。当前版本已经是上限时，本次 mutation 在构造任何新状态前以
`version_overflow` 原子拒绝，不产生 Python 可表示但 SQLite 无法持久化的版本。

为什么 CAS：
- **并发安全**：A B 两端同时改 entity，后到的发现 version 已变，明确收到冲突信号；不会"静默覆盖"
- **AI 长任务保护**：AI 生成时先存 expected_version，完成后回写；期间用户改了 entity，AI 的 patch 被拒，避免 AI 覆盖用户最新输入
- **幂等性**：重试同一 expected_version 不会重复执行；UI 乐观锁基础

冲突解决策略由调用方决定：reject / merge / 重新基于最新版本重试。本叶子不内置策略，只提供检测。

## 4. 配套值类型

### Actor

```python
@dataclass(frozen=True)
class Actor:
    user_id: str
    device_id: str = "default"
    source: str = "user"   # user | ai | sync | system
```

`source` 四态用于：
- `user` — 真人操作
- `ai` — AI 自动产出（pipeline、generation 等）
- `sync` — 远端 commit 通过协作通道同步进来
- `system` — 自动化任务（定时、迁移、清理）

每次 update/delete/restore 都把当前 Actor 记进 `_actor_log`，构成审计链路。**Operation 叶子启动后 `Operation.source` 也用同一份 Actor 类型**（`from entity import Actor`）。

### AiProvenance

```python
@dataclass(frozen=True)
class AiProvenance:
    actor: str                       # user_id（触发者）
    model: str                       # gpt-4 / claude-opus-4-7 / ...
    model_version: str = ""
    prompt_template_id: str = ""
    prompt_args: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    token_in: int = 0
    token_out: int = 0
    ts: float = 0.0
    trace_id: str = ""               # 跨服务日志聚合用
```

调 update 时通过 `ai_provenance={"field_name": AiProvenance(...)}` 把 AI 溯源信息挂到对应 field。`AiProvenance.__post_init__` 深层持有 `prompt_args`；update 再复制整条 provenance。复制时要求 exact class 且实例 `__dict__` 恰好包含声明的 10 个字段，额外/缺失/未来字段不会被静默丢弃。后续查 `entity.provenance("field_name")` 拿到 detached `AiProvenance`（界面上显示"此字段由 GPT-4 生成，花费 0.0023 USD"）。

## 5. schema 校验（Validator 协议注入）

Entity 的 `data` 字段类型 / 必填 / 约束校验，由 `04-field-types` 叶子提供真实现。本叶子用**协议注入**保持自治：

```python
Validator = Callable[[dict, dict, str], None]
# (data, schema, path) -> None；不通过抛 ZeError("schema_violation")
```

构造和 update 都接受可选 `validator=` 参数：

- 不传 → 默认 `_NO_OP_VALIDATOR`，无校验（适合 prototyping / 不带 schema 的 entity）
- 传真校验器 → 构造时跑一次；update 时对**完整候选 data**只跑一次。root required、
  `additionalProperties:false` 与跨字段 oneOf/allOf 都不能被 patch 子集绕过
- update 的 `validator=` 仅为签名兼容：只能省略或传与已安装 validator 相同的对象；
  不同 identity（包括 custom no-op）在任何状态写入前稳定拒绝，不能单次绕过 authority

undo/redo 恢复旧 schema 之前的历史值走独立 framework-private replay seam；它不借用
public `validator=` 充当 bypass token，仍执行 strict ownership、CAS、完整 durable envelope
预算与 actor log，只跳过“用今天 schema 重验昨天状态”。

Entity 保留传入 callable 的对象 identity（不用 truthiness 选择，也不复制 callable）。
任何非 `None` validator 都必须通过 `callable()` 检查，否则抛 payload-free
`validation_failed`。
传给 callable 的 data / schema 是 detached 校验视图，validator 留存或修改它们不会取得
Entity 内部容器所有权。

standalone Entity 可以使用 custom validator；一旦进入 Document，validator authority
只属于 Document：public `add` 按对应 type schema 重装 framework validator 并立即校验，
内部 restore/bootstrap 用 `attach_validator` 重装但不重验历史数据；type schema 空或缺失
时统一清成 exact no-op，custom 不得跟随 Entity 进入 Document。本叶子不依赖
04-field-types，靠 Validator 协议保持自治。

## 6. Python API

每个方法标 ✅（已实装）或 ⏳（待补，标注被依赖的叶子）。详见 §11。

```python
from entity import Entity, Actor, AiProvenance, Validator

# ──────── Entity 本体（全部已实装 ✅）────────

# 构造
e = Entity(
    type="object",                    # ✅ 必填
    data={...},                       # ✅ 必填
    name="样例物体",                  # ✅
    id=None,                          # ✅ 默认 uuid4 hex
    schema={...},                     # ✅ 校验规则；跟 validator 配套
    validator=None,                   # ✅ 默认 no-op；04 完成后 caller 传真校验器
    read_only=False,                  # ✅ 默认 False
)

# 只读 property（JSON 容器返回 deep detached copy）
e.id / e.type / e.name / e.version / e.deleted / e.read_only / e.data       # ✅
e.created_at / e.updated_at                                                  # ✅

# 读取助手
e.get(key, default=None)                                                     # ✅ data 内字段
e.provenance(field_name) -> AiProvenance | None                              # ✅
e.actor_log() -> list[dict]                                                  # ✅ 完整变更日志

# 变更（全部 CAS）
e.update(patches, *, expected_version, actor,
         ai_provenance=None, name=None, validator=None) -> Entity            # ✅
e.delete(*, expected_version, actor) -> Entity                               # ✅ 软删
e.restore(*, expected_version, actor) -> Entity                              # ✅ 恢复

# 持久化（完整 round-trip：version/log/provenance/timestamps 保真）
e.to_dict() -> dict                                                          # ✅
Entity.from_dict(d, *, schema=None, validator=None) -> Entity                # ✅ 不走 update, 直接恢复内部状态
```

## 7. 软删 vs 硬删

| 操作 | 谁能做 | 行为 |
|---|---|---|
| **软删** `entity.delete()` | 本叶子能力（CAS）| `_deleted=True`，version +1，actor_log 加一条 `{fields: ["__deleted__"]}` |
| **恢复** `entity.restore()` | 本叶子能力（CAS）| `_deleted=False`，version +1，actor_log 加 `{fields: ["__restored__"]}` |
| **硬删** `doc.hard_remove(type, id)` | **Document 侧**能力 | 物理删除；不查 read_only；级联 / 管理员 / 测试用 |

软删保留 entity 数据，可恢复；硬删立即丢失。日常 UI 操作用软删；管理员 / cascade_delete 用硬删。

## 8. 持久化（round-trip）

`to_dict()` 序列化全部字段：

```json
{
  "id": "...",
  "type": "object",
  "name": "样例物体",
  "version": 5,
  "deleted": false,
  "read_only": false,
  "data": {...},
  "provenance": {
    "description": {"actor": "u1", "model": "gpt-4", "cost_usd": 0.002, ...}
  },
  "actor_log": [
    {"version": 2, "actor": {"user_id": "u1", "source": "user"}, "ts": 1.0, "fields": ["name"]},
    ...
  ],
  "created_at": ...,
  "updated_at": ...
}
```

`Entity.from_dict()`：
- **不走 update()**：直接恢复 `_version` / `_actor_log` / `_provenance` / `_created_at` / `_updated_at`
- **保真**：round-trip 后 version 不变、log 不加新条目、时间戳一致
- **深层隔离**：先复制并校验持久化输入；后续修改输入 dict / 嵌套 list 不影响恢复对象
- **legacy metadata 宽容**：version / deleted / read_only / timestamps 保留旧的标量转换，但只对 exact JSON scalar 执行；对象及子类不能触发 `__int__` / `__float__` / `__bool__` hook
- AiProvenance 重建为 frozen dataclass；额外 key 容错（forward-compat 丢弃）
- `read_only=True` 保留
- **完整 envelope 闭合**：最终候选使用与 `to_dict()` 相同的 250,000 节点预算；精简
  输入本身可复制但补齐 metadata 后越界时仍拒绝

## 9. Mod 端必填 + 注册

mod 端在 `02-mod/02-entities.md` 声明本 skill 的 entity_type 清单 + 每个 type 的 schema + 协作粒度；在 `02-mod/05-code/` 实现 `register()` 函数：

```python
from sdk import DocumentRegistry

def register(registry: DocumentRegistry) -> None:
    registry.declare_entity_type(
        name="object",
        schema={
            "name": {"type": "string", "required": True},
            "bbox": {"type": "list", "items": "number", "length": 4},
        },
        collaboration_unit=True,    # 这种 entity 是独立协作单元（CRDT 用）
    )
```

framework 启动时 `registry.build_document()` 把所有声明灌进新建的 Document，Document.add(entity) 时按声明取对应 schema + validator。

## 10. 红线规则 + 反模式

红线（违反抛 ZeError）：

- **不能直接 mutate `entity._data`** — 必须走 `update(patches, expected_version, actor)`
- **read_only entity 不能写** — update / delete / restore 全抛 `read_only_violation`（AP-E1）
- **软删后不能 update** — 抛 `entity_soft_deleted`；要变更先 `restore` 再 update（AP-E2）
- **CAS 失败抛 cas_version_mismatch** — caller 决定重试 / merge / reject
- **业务值必须是 strict JSON tree** — subclass / 非有限数 / surrogate / cycle / 过深容器统一抛不含 payload 的 `validation_failed`
- **跨 Document 引用禁** — 跟 SPEC §8 一致；entity 的 `$ref` 只能指本 doc 内的 entity（AP-E3）

反模式：

- **AP-E1** 写 read_only entity → 在 dispatcher 层拦（Gate 37），entity 层抛错兜底
- **AP-E2** 软删后试图 update → 应先 restore；或选择 hard_remove 走流程
- **AP-E3** entity.data 引用别 doc 的 entity → 不支持；要跨就走 cross-skill-call
- **AP-E4** entity.data 里塞 binary（图片字节）→ 应走 asset_ref（03-asset 叶子）
- **AP-E5** 把大同质数组每元素拆 entity（瓦片地图 1000×1000） → 整数组打包成一个 entity
- **AP-E6** 跳过 CAS 自己 mutate `_data` / `_version` → 破坏审计链路 + 协作冲突检测

## 11. 当前实装状态

### ✅ 已实装

| 项 | 说明 |
|---|---|
| 构造 + 必填校验 | `type` + `data` 必传；id 默认 uuid4 |
| strict JSON 值所有权 | exact builtin + 4300 位 int + finite float + UTF-8 + 120 层 + 250,000 展开节点；拒绝 cycle/subclass；不用 deepcopy/hooks；稳定 payload-free 错误 |
| 元属性 property | id / type / name / version / deleted / read_only / data / created_at / updated_at（容器深层 detach，避免内部暴露）|
| CAS `update(patches, *, expected_version, actor, ai_provenance=None, name=None)` | 版本不匹配抛；read_only / 软删拒；patches 合并 data；完整候选 data 校验一次；完整 durable envelope 预检后原子提交 name/provenance/log/version/timestamps |
| CAS `delete(*, expected_version, actor)` | 软删 + version bump + actor_log 加 `{fields:["__deleted__"]}`；read_only 拒 |
| CAS `restore(*, expected_version, actor)` | 恢复 + version bump + actor_log 加 `{fields:["__restored__"]}`；未删 idempotent；read_only 拒 |
| 读取助手 | `get(key, default)` / `provenance(field)` / `actor_log()`；命中值、provenance、审计日志均深层 detach |
| `Actor` 值类型 | frozen dataclass（user_id / device_id / source）|
| `AiProvenance` 值类型 | frozen dataclass（model / cost / tokens / trace_id / prompt_template）；`prompt_args` 入站持有、出站 detach |
| Validator 协议 | standalone 构造可注入 callable；默认 no-op；保留 identity，参数与内部隔离；update 校验完整候选 data 且 per-call 参数不得换 identity |
| `install_validator` / `attach_validator` | Framework 内部 hook；前者立即校验，后者只安装且原子准备 schema+callable；进入 Document 后均由 Document authority 覆盖 custom |
| `has_validator` property | 检查是否注入过真校验器（非 no-op）|
| `to_dict()` / `Entity.from_dict()` | 完整 round-trip — version / actor_log / provenance / timestamps / read_only / deleted 全保真；构造/恢复/每次 mutation 共用完整 envelope 节点预检 |
| 真 schema 校验器（已接入）| Document.add(entity) 按 entity.type 无条件重装 authority；非空 schema 立即校验、空/缺 schema exact no-op；后续 update 对完整候选 data 跑 field-types 校验 |

### ⏳ 待补（真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| `asset_ref` 字段类型识别 | ✅ `04-field-types` 已支持 `{type: asset_ref}` 校验 sha256+mime+size+ext 形态 |
| Operation 跟 Actor 协同 | ✅ `02-operation` 已实施；Operation.actor 复用本叶子 Actor.to_dict() |
| computed 字段（声明式 + 引擎）| `02-data-flow/04-history-rollback` 字段历史驱动重计算 |
| crdt 字段（声明式 + 引擎）| `02-data-flow/03-offline-first` CRDT 引擎 |

### 原则

- **签名先定下来**：⏳ 占位的部分（如 validator）签名固定，依赖叶子完成后只补 body / 注入逻辑
- **API 不破坏**：本叶子作为底层，签名变更影响所有上层调用方
- **测试覆盖 ✅ 全部**：⏳ 部分通过 mock validator 验证集成点签名稳定
