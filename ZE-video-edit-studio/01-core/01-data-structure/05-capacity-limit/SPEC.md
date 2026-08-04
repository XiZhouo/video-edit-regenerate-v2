# Capacity Limit — 容量上限

## 1. 这是什么

`CapacityLimits` + `CapacityChecker`：单 Document 的**数据规模上限**护栏。Gate 74（容量上限护栏）真校验。

业务白话视角：业务同事在 ZE-seek-find-object-generator 上批量导入 5 万张场景图——entity 数会爆 1 万的默认上限。framework 要在 batch handler 入口告诉业务同事"超了，要么分批，要么显式抬上限"，而不是让 Document.add 安静撑大到内存炸。

### 为什么是独立叶子

| 问题 | 原因 |
|---|---|
| **强耦合到 hot path 会破坏现有测试** | 把 check 嵌进 `Document.add` / `entity.update` / `store.put` 会让 418 个测试全要造 caller 配额上下文；admin / 批量导入场景需要临时放宽，硬嵌死路一条 |
| **限额值场景化** | 单机/单 Doc 默认 1 万 entity 够用；大型项目 / 服务化部署需要按 env 调整；硬编码不够 |
| **诊断 vs 拒绝分离** | `measure_entity` 给查诊用（"我这个 entity 多大？多少字段？"），`check_*` 给拒绝用（超限抛 ZeError）；混在一起容易把单纯的查询路径污染成"读一下就拒了" |
| **预防被 silent 失败盖住** | 没有 check_*，超大的 entity 落进 entity.json 后只是写盘慢/前端卡顿，没人会发现；显式 check 把超限路径变成 caller 必处理的错误 |

## 2. 限额清单

五类资源：

| 字段 | 默认 | 单位 | 用处 |
|---|---|---|---|
| `entity_count_max` | 10_000 | 个 | 单 Document 容纳 entity 上限（含软删，不含 hard_removed）|
| `entity_data_size_max` | 1_000_000 | 字节（JSON 序列化大小）| 单 entity.data 最大体积 |
| `entity_field_count_max` | 100 | 个 | 单 entity.data 顶层字段数（递归键总数）|
| `asset_blob_size_max` | 536_870_912 | 字节（512 MB）| 单 blob 文件最大体积 |
| `doc_total_assets_size_max` | 536_870_912 | 字节（512 MB） | 单 Doc 本地唯一 blob 总体积上限；共享池不重复计入 |

**为什么默认值是这些**：

- 1 万 entity — 内存里 `Map[(type, id), Entity]` 在 1 万级别遍历 O(n) 几毫秒；超过会让 `doc.list / doc.find` 卡顿
- 1 MB entity.data — 单条 entity JSON 序列化超 1 MB 通常意味着误塞了 binary 进 entity（违反 [03-asset/ AP-E4](../03-asset/SPEC.md#10-红线规则--反模式)）；阈值兼顾"长文本编辑器场景" + "明显异常"
- 100 字段 — entity schema 字段数 100 个已经接近不可读上限；超出大概率是错把 dict-of-dict 当 entity 用，应拆成 sub-entity
- 512 MB blob — 单文件 512 MB 是常见的"视频/3D 模型"上限；再大需要分片上传（待 12-file-search）

## 3. CapacityLimits 值类型

```python
@dataclass(frozen=True)
class CapacityLimits:
    entity_count_max: int = 10_000
    entity_data_size_max: int = 1_000_000
    entity_field_count_max: int = 100
    asset_blob_size_max: int = 512 * 1024 * 1024     # 512 MB
    doc_total_assets_size_max: int = 512 * 1024 * 1024

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CapacityLimits":
        """从环境变量读, 缺省 fallback 到 default.

        识别的环境变量:
          ZE_CAPACITY_ENTITY_COUNT_MAX
          ZE_CAPACITY_ENTITY_DATA_SIZE_MAX
          ZE_CAPACITY_ENTITY_FIELD_COUNT_MAX
          ZE_CAPACITY_ASSET_BLOB_SIZE_MAX
          ZE_CAPACITY_DOC_TOTAL_ASSETS_SIZE_MAX

        所有值必须 > 0 整数; 非法值抛 ZeError("capacity_config_invalid").
        env 参数为 None 时默认读 os.environ; 显式传 dict 便于测试.
        """
```

**为什么 frozen**：CapacityLimits 是配置类值对象，运行期常 copy / 比较 / 作 dict key 候选；frozen 防误改 + 让 hash 稳定。

**为什么 `from_env(env=None)` 而不是 `@classmethod from_env(cls)`**：测试需要注入 env dict 不污染进程 `os.environ`；prod 用 None 走默认。

**为什么不给 env_prefix 参数自定义**：v3 阶段单 prefix `ZE_CAPACITY_` 够用；多 prefix 是 YAGNI 信号。

## 4. DEFAULT_LIMITS

```python
DEFAULT_LIMITS: CapacityLimits = CapacityLimits()
```

模块顶层导出。caller 不传 limits 时 `CapacityChecker(DEFAULT_LIMITS)`；测试调 `CapacityChecker(CapacityLimits(entity_count_max=10))` 临时收紧。

## 5. CapacityChecker

```python
class CapacityChecker:
    def __init__(self, limits: CapacityLimits = DEFAULT_LIMITS) -> None: ...

    # ──────── 拒绝路径 (超限抛 ZeError("capacity_exceeded")) ────────
    def check_add_entity(self, doc) -> None
        """当前 doc.list_all() 数 + 1 是否超 entity_count_max."""

    def check_entity_data(self, entity) -> None
        """同时校 data_size + field_count; 任一超就抛."""

    def check_asset_blob(self, size: int) -> None
        """blob 字节数 > asset_blob_size_max 抛."""

    def check_doc_total_assets(self, store, *, additional_size=0) -> None
        """当前本地唯一 blob + 拟新增字节不得超过 project 总额."""

    # ──────── 诊断路径 (只测量, 不抛) ────────
    def measure_entity(self, entity) -> dict
        """返回 {'data_size': int, 'field_count': int}."""
```

### 5.1 抛错的形态

```python
raise ZeError(
    "capacity_exceeded",
    context={
        "limit_name": "entity_count_max",
        "limit": 10_000,
        "actual": 10_001,
    },
)
```

- `limit_name` 必填，让 caller 知道"哪条限超了"
- `limit` + `actual` 数值字段；caller 报错信息可直接 format
- 不抛多层异常套娃；一个超限一个 ZeError

### 5.2 measure_entity 算什么

```python
{
    "data_size": len(json.dumps(entity.data, ensure_ascii=False).encode("utf-8")),
    "field_count": <递归计算所有 key 总数>,
}
```

**为什么用 JSON UTF-8 字节数**：跟实际落盘成本对齐（entity.json 就是这么序列化的）；不用 `sys.getsizeof`（Python 内存表示 != 实际持久化大小）。

**为什么字段数递归**：嵌套 dict 也算字段。`{"a": {"b": 1, "c": 2}, "d": 3}` 字段数 = 4（a / a.b / a.c / d）。

## 6. 怎么集成到 caller

**关键决策**：**不**嵌入 hot path。`Document.add` / `entity.update` / `store.put` 默认**不**调 checker。

理由：

- **保留 admin 通道** — 系统级清理 / 数据迁移 / 强制 import 需要绕开限额
- **保留批量场景** — 业务方批量导入 5 万行可以一次性提升 limit 跑完再恢复
- **不破坏现有测试** — 418 个测试不需要为 capacity 改造

**集成路径**（caller 显式调）：

```python
from capacity import CapacityChecker, CapacityLimits, DEFAULT_LIMITS
from document import Document
from entity import Entity, Actor

checker = CapacityChecker()  # = CapacityChecker(DEFAULT_LIMITS)

# 业务 command handler 入口主动校验
def entity_add_handler(doc, payload, actor):
    new_entity = Entity(type=payload["type"], data=payload["data"])
    checker.check_add_entity(doc)         # 数量是否超
    checker.check_entity_data(new_entity) # data 大小 / 字段数是否超
    # ↑ 超限抛 capacity_exceeded; caller 不接住就冒上去给 UI
    doc.add(new_entity)
    return new_entity

# Asset 上传入口
def asset_upload_handler(store, file_path):
    size = os.path.getsize(file_path)
    checker.check_asset_blob(size)
    return store.put_file(file_path)
```

**何时跳过 checker**：

| 场景 | 跳法 |
|---|---|
| Admin 命令 | command handler 不调 check_*；source=system 路径绕开 |
| 临时放宽（batch import） | `CapacityChecker(CapacityLimits(entity_count_max=100_000))` 单次构造 |
| 测试 | 测自身用 tight limits；测业务路径用 default |

**未来强化路径**：在 [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md) 加 `GATE-CAP-*` runtime gate；发版前必跑（取生产数据 + checker 全跑一遍），把"是否超"作为发布卡点。

## 7. 跟其他叶子的关系

| 叶子 | 关系 |
|---|---|
| [01-document/](../01-document/) | check_add_entity 调 `doc.list_all()` 数 entity；不修改 Document API |
| [02-entity/](../02-entity/) | measure / check_entity_data 读 `entity.data`；不修改 Entity API |
| [03-asset/](../03-asset/) | `total_size()` 严格汇总本地唯一 blob；check_asset_blob / check_doc_total_assets 分别限制单件与项目总额 |
| [04-field-types/](../04-field-types/) | 不依赖；field-types 校"形态"，capacity 校"规模"；正交 |
| [_protocol/](../../_protocol/) | 共享 `ZeError`（从 entity 模块 import）；audit gate 注册 |
| commands-operations | command handler 是 capacity check 的主调用点；本叶子不强制 |

## 8. 持久化 / 配置协议

**limits 本身不持久化到 document.json**。理由：

- limits 是**部署级配置**，不是**文档内容**
- 同一份 document.json 在不同部署（开发机 / 服务器）配额不同合理
- 避免把 caller 的"运维决策"塞进数据

**配置来源优先级**：

1. caller 显式传 `CapacityLimits(...)` — 最高
2. `CapacityLimits.from_env()` — 部署时 `ZE_CAPACITY_*` env
3. `DEFAULT_LIMITS` — fallback

**from_env 错误处理**：

- 缺失：fallback 到 default 字段值
- 非整数：抛 `ZeError("capacity_config_invalid", context={"field":..., "value":...})`
- ≤ 0：同上抛（容量必正）

## 9. audit gates

注册到 [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md)：

| Gate | 名字 | 检查什么 | 形态 |
|---|---|---|---|
| `GATE-CAP-01` | capacity_limits_from_env | env 变量正常读取 + 非法值抛 capacity_config_invalid | runtime |
| `GATE-CAP-02` | capacity_exceeded_throws | 4 个 check_* 函数超限均抛 capacity_exceeded + context 含 limit/actual | runtime |
| `GATE-CAP-03` | capacity_measure_entity | measure_entity 返回字段齐全 + 数值准确 | runtime |

## 10. 红线规则 + 反模式

红线：

- **不嵌入 framework hot path** — Document.add / entity.update / store.put 不调 checker；caller 显式调
- **limits 不持久化到 document.json** — 部署级配置不混进文档数据
- **check_* 超限只抛 `capacity_exceeded`** — 不抛多种错误码；caller 按 limit_name 分支

反模式：

- **AP-C1** 直接改 `CapacityLimits` 字段（绕过 frozen）→ 应构造新实例
- **AP-C2** 把 limits 写进 `Document(schema_version, capacity_limits=...)` 参数 → 配置耦合到数据
- **AP-C3** check_* 失败时 caller catch 后默默继续 → 应让错误冒出或显式 log + 拒绝
- **AP-C4** measure_entity 用 `sys.getsizeof(entity.data)` → 跟实际持久化大小差几倍，统计失真
- **AP-C5** 在 `Document.add` 内部偷偷 hook capacity check → 破坏"caller 显式调"协议；测试会大量失败
- **AP-C6** 把 `from_env()` 的 env_prefix 做成可参数 → YAGNI；单 prefix `ZE_CAPACITY_` 锁死
- **AP-C7** 业务方接入"深嵌套 entity.data"（>900 层 `{"a":{"a":...}}`）→ `_field_count_recursive` 跟 `json.dumps` 都吃 Python 默认 1000 层递归栈；实际生产里 `entity_data_size_max=1MB` 会先拦下；caller 业务层不应构造这种数据

## 11. 当前实装状态

### ✅ 已实装

| 项 | 说明 |
|---|---|
| `CapacityLimits` frozen dataclass | 4 个 int 字段 + 默认值 |
| `CapacityLimits.from_env(env=None)` | 读 `ZE_CAPACITY_*`；缺失 fallback；非法抛 `capacity_config_invalid` |
| `DEFAULT_LIMITS` 模块常量 | `= CapacityLimits()` |
| `CapacityChecker.check_add_entity(doc)` | doc.list_all() 数 + 1 > entity_count_max 抛 |
| `CapacityChecker.check_entity_data(entity)` | data_size > entity_data_size_max 或 field_count > entity_field_count_max 抛 |
| `CapacityChecker.check_asset_blob(size)` | size > asset_blob_size_max 抛 |
| `CapacityChecker.measure_entity(entity)` | 返回 {data_size, field_count} |
| ZeError 共享 | 从 `entity` 模块 import（跟 document / asset 一致）|
| audit gates 3 个 | GATE-CAP-01..03 |

### ⏳ 待补（真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| HTTP/project asset 写入消费 `check_doc_total_assets(store, additional_size=...)` | 已实施；去重命中 additional_size=0，坏 meta fail-closed |
| `capacity_check` 自动 hook | command handler / pipeline 启动后由业务方接入 |
| 监控指标导出（接 Prometheus）| 工程化阶段；本叶子提供数据，导出走外层 |
| 分布式 quota（多 Doc 总和上限）| 多 Doc / 多租户场景启动后 |

### 原则

- **API 签名先定下来**：`check_*` / `measure_*` / `from_env` 后续不会变签名
- **caller 显式调**：不自动 hook 到 framework，保留 admin / batch 绕路
- **限额数值可调**：默认值仅作起点；真生产数据决定如何调整
