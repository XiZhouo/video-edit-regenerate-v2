# 04-field-types — Field Types

> 一句话：**JSON Schema 驼峰风格字段校验器**。提供 entity.data 的类型 / 约束 / 必填校验，Document.add 时自动注入。

## 在框架里的位置

- 上一级：[01-data-structure/](../) — 数据结构 5 个叶子
- 同级：[01-document/](../01-document/) / [02-entity/](../02-entity/) / [03-asset/](../03-asset/) / [05-capacity-limit/](../05-capacity-limit/)
- 接口对象：[02-entity/](../02-entity/) `Validator` 协议（`Callable[[dict, dict, str], None]`）

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览）|
| [SPEC.md](./SPEC.md) | 11 节完整规则（类型清单 / 约束 / asset_ref / entity_ref / make_validator / 集成）|
| [field_types.py](./field_types.py) | validate + make_validator + 类型校验 |
| [test_field_types.py](./test_field_types.py) | pytest（67 测试）|

## 当前状态

**✅ 已实装**（全部无跨叶子依赖项 + 完整集成）

类型：
- 基础 6 个：`string` / `number` / `integer` / `boolean` / `array` / `object`
- 特殊：`null`（配合 oneOf 做 nullable）/ 复合 `oneOf` / `anyOf` / `allOf` / 无 type 通过
- ZE 专有：`asset_ref` / `entity_ref`

约束：
- string: `minLength` / `maxLength` / `pattern` / `enum` / `format`（email / uri / uuid / iso_datetime）
- number: `minimum` / `maximum` / `exclusiveMin/Max` / `multipleOf`
- array: `minItems` / `maxItems` / `uniqueItems` / `items` / `itemSchema`（v2 别名）
- object: `properties` / `required`（双风格兼容）/ `additionalProperties`

关键设计：
- ✅ `required` 双风格：父对象 `required` 数组（JSON Schema 标准）+ 字段上 `required: True`（v2 / mod 模板风格）都接受
- ✅ `bool` 不视为 `number`（Python `isinstance(True, int)=True` 坑修复）
- ✅ `asset_ref` 形态校验（sha256 64 hex + mime/size/ext 类型），不查 blob 真存在
- ✅ `entity_ref` 形态校验（`{$ref: "type/id"}` + 可选 `to` 类型限制），不查 entity 真存在
- ✅ `make_validator(schema)` 是唯一 Entity-schema 规范化边界：canonical 内部形态是 root object/composite；legacy flat field-map 仅在工厂入口确定性包成 `type: object + properties`，Document 存储原文不迁移
- ✅ framework wrapper 持有隔离后的规范化 schema，调用时始终使用自己的 authority，协议里的 caller schema 不能把它覆盖成 raw flat 或宽松子集；私有 `is_framework_validator(value, expected_schema=None)` 既可只认 exact wrapper，也可校对其绑定的规范化 authority
- ✅ Document.add 单一权威：无条件覆盖 standalone custom；非空 type schema 立即校验、空/缺 schema exact no-op；Entity.update 每次对完整候选 data 校验一次且拒绝 per-call 换 validator，root required / additionalProperties / 跨字段 composite 都生效

**⏳ 待补**（真有跨叶子依赖）

- `computed` 字段（声明 + 引擎）— 等 [04-history-rollback/](../../02-data-flow/04-history-rollback/) 字段历史驱动重计算
- `crdt` 字段（声明 + 引擎）— 等 [03-offline-first/](../../02-data-flow/03-offline-first/) CRDT 引擎

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态)。

## 怎么用

### 直接调 validate

```python
from field_types import validate

validate("hello", {"type": "string", "minLength": 1})
validate({"sha256": "a" * 64, "mime": "image/png"}, {"type": "asset_ref"})
validate({"$ref": "obj/e1"}, {"type": "entity_ref", "to": "obj"})

# 失败抛 ZeError("schema_violation")
validate("", {"type": "string", "minLength": 1})   # 抛
```

### 通过 Document.add 自动注入（推荐路径）

```python
from document import Document, Entity

# 业务方在 mod register() 里声明 schema
doc = Document(
    schema_version="1.0.0",
    collaboration_mode="with_save",
    schema={
        "entry": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "required": True,
                          "minLength": 1, "maxLength": 200},
                "tags": {"type": "array", "items": {"type": "string"}},
                "image": {"type": "asset_ref"},
            },
        },
    },
)

# add 时自动注入 validator + 立即校验
doc.add(Entity(type="entry", data={"title": "Hello", "tags": ["x"]}))
# OK

doc.add(Entity(type="entry", data={"title": ""}))
# 抛 ZeError("schema_violation": "$entry.title: minLength 1")

# update 时也自动跑校验
e = doc.get("entry", "e1")
e.update({"title": ""}, expected_version=e.version, actor=actor)
# 抛 ZeError
```

### nullable 字段

```python
schema = {
    "type": "object",
    "properties": {
        "deletedAt": {"oneOf": [{"type": "string"}, {"type": "null"}]},
    },
}
```

## 设计要点

- **纯函数** — `validate` 无副作用 / 可并发调
- **不查存在性** — `asset_ref` / `entity_ref` 只校形态，真存在性归 Document / AssetStore
- **canonical root 唯一** — framework validator 内部只持有 object/composite root；legacy flat 只在工厂单点兼容，避免运行期双语义
- **bool ≠ number** — 修 Python `isinstance(True, int)` 坑
- **required 双风格** — JSON Schema 标准 + v2 / mod 模板风格都吃
- **完整候选校验** — update 不按 patch 切 schema，root 和跨字段约束不会漏检

## 跟 audit gate 关系

| Gate | 验证 |
|---|---|
| `GATE-FT-01` | string type 拒非 str |
| `GATE-FT-02` | required 风格双兼容 |
| `GATE-FT-03` | asset_ref 形态 |
| `GATE-FT-04` | entity_ref 形态 + to 限制 |
| `GATE-FT-05` | nullable oneOf null |
| `GATE-FT-06` | bool 不视为 number |

后续加入 [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md) 注册表。

## 跑测试

```bash
cd 01-core/01-data-structure/04-field-types/
python -m pytest test_field_types.py -v        # 67 测试
```
