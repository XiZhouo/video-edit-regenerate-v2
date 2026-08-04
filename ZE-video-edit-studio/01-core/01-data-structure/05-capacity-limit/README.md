# 05-capacity-limit — Capacity Limit

> 一句话：**数据规模上限护栏**。单 Document 多少 Entity、单 Entity.data 多大、单 blob 多大；超限抛 `capacity_exceeded`。

## 在框架里的位置

- 上一级：[01-data-structure/](../) — 数据结构 5 个叶子
- 同级：[01-document/](../01-document/) / [02-entity/](../02-entity/) / [03-asset/](../03-asset/) / [04-field-types/](../04-field-types/)
- 集成对象：业务方 command handler / batch import 路径（caller 显式调，不嵌 hot path）

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览）|
| [SPEC.md](./SPEC.md) | 11 节完整规则（限额清单 / from_env / 4 个 check / measure / 集成）|
| [capacity.py](./capacity.py) | CapacityLimits + CapacityChecker + DEFAULT_LIMITS |
| [test_capacity.py](./test_capacity.py) | pytest（39 测试）|

## 当前状态

**✅ 已实装**（全部无跨叶子依赖项）

- `CapacityLimits` frozen dataclass — 5 个 int 字段
- `CapacityLimits.from_env(env=None)` — 读 `ZE_CAPACITY_*` 环境变量；非法值抛 `capacity_config_invalid`
- `DEFAULT_LIMITS = CapacityLimits()` 模块常量
- `CapacityChecker`：
  - `check_add_entity(doc)` — 当前 entity 数 + 1 > entity_count_max 抛
  - `check_entity_data(entity)` — data_size / field_count 任一超抛
  - `check_asset_blob(size)` — size > asset_blob_size_max 抛
  - `measure_entity(entity)` — 返 {data_size, field_count}（诊断路径，不抛）
- 超限统一抛 `ZeError("capacity_exceeded", context={limit_name, limit, actual})`
- ZeError 共享：从 02-entity import（跟 document / asset 一致）

**⏳ 待补**（真有跨叶子依赖）

- `check_doc_total_assets(store, additional_size=0)` — 读取 AssetStore 的严格本地总量并在写前校验拟新增字节
- 自动 hook 到 command handler — 等 [03-commands-operations/](../../03-commands-operations/) 业务方接入
- 监控指标导出（Prometheus 等）— 工程化阶段；本叶子提供数据，导出走外层

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态).

## 关键设计决策

- **不嵌入 hot path** — `Document.add` / `entity.update` / `store.put` 默认**不**调 checker；caller 显式调
  - 理由：保留 admin 通道 / batch 临时放宽 / 不破坏现有 418 测试
- **诊断 vs 拒绝分离** — `measure_*` 只测量不抛；`check_*` 超限即抛；混在一起会污染查询路径
- **JSON UTF-8 字节数** — `data_size` 跟实际落盘成本对齐，不用 `sys.getsizeof`
- **字段数递归** — 嵌套 dict / list 内 dict 都算入 field_count；上限 100 保护"扁平 entity"原则
- **配置不持久化到 document.json** — limits 是部署级配置，不是文档内容；同份 doc 在不同部署用不同 limits 合理

## 默认值

| 字段 | 默认 |
|---|---|
| `entity_count_max` | 10_000 个 |
| `entity_data_size_max` | 1_000_000 字节（1 MB JSON UTF-8）|
| `entity_field_count_max` | 100 个（递归键总数）|
| `asset_blob_size_max` | 536_870_912 字节（512 MB）|
| `doc_total_assets_size_max` | 536_870_912 字节（512 MB）|

选型理由见 [SPEC.md §2](./SPEC.md#2-限额清单)。

## 怎么用

### 业务 command handler 入口（推荐路径）

```python
from capacity import CapacityChecker, CapacityLimits
from document import Document
from entity import Entity, Actor

checker = CapacityChecker()  # 用 DEFAULT_LIMITS

def entity_add_handler(doc, payload, actor):
    new_entity = Entity(type=payload["type"], data=payload["data"])
    checker.check_add_entity(doc)         # 数量超抛 capacity_exceeded
    checker.check_entity_data(new_entity) # data 大小 / 字段数超抛
    doc.add(new_entity)
    return new_entity
```

### Asset 上传入口

```python
import os
size = os.path.getsize(file_path)
checker.check_asset_blob(size)            # 大小超抛
checker.check_doc_total_assets(store, additional_size=size)  # 项目总额超限抛
ref = store.put_file(file_path)
```

### 从环境变量读 limits（部署时）

```bash
export ZE_CAPACITY_ENTITY_COUNT_MAX=50000
export ZE_CAPACITY_ASSET_BLOB_SIZE_MAX=1073741824   # 1 GB
export ZE_CAPACITY_DOC_TOTAL_ASSETS_SIZE_MAX=2147483648  # 2 GB / project
```

```python
limits = CapacityLimits.from_env()
checker = CapacityChecker(limits)
```

### 诊断（不抛）

```python
m = checker.measure_entity(entity)
# {"data_size": 12345, "field_count": 42}
```

### 临时放宽（batch import）

```python
batch_limits = CapacityLimits(entity_count_max=100_000)
batch_checker = CapacityChecker(batch_limits)
for row in csv_rows:
    batch_checker.check_add_entity(doc)
    doc.add(Entity(type="row", data=row))
```

## 跟其他叶子的关系

| 叶子 | 关系 |
|---|---|
| [01-document/](../01-document/) | check_add_entity 数 `doc.types() + doc.list(t, include_deleted=True)`；不修改 Document API |
| [02-entity/](../02-entity/) | measure / check_entity_data 读 `entity.data`；不修改 Entity API |
| [03-asset/](../03-asset/) | check_asset_blob 校单件；`total_size()` + check_doc_total_assets 校项目本地总量 |
| [04-field-types/](../04-field-types/) | 不依赖；field-types 校"形态"，capacity 校"规模"；正交 |
| [_protocol/](../../_protocol/) | 共享 `ZeError`；3 个 audit gate（GATE-CAP-01..03）注册 |

## 跟 audit gate 关系

| Gate | 验证 |
|---|---|
| `GATE-CAP-01` | `from_env` 正常读 + 非法值抛 `capacity_config_invalid` |
| `GATE-CAP-02` | 4 个 `check_*` 超限均抛 `capacity_exceeded` + context 齐 |
| `GATE-CAP-03` | `measure_entity` 数值准确（JSON UTF-8 字节 + 递归字段数）|

注册在 [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md)。

## 跑测试

```bash
cd 01-core/01-data-structure/05-capacity-limit/
python -m pytest test_capacity.py -v        # 39 测试

# 跑 audit gates
python ../../_protocol/audit_gates.py --leaf CAP
```
