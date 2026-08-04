# Field Types — 字段类型 + JSON Schema 校验

## 1. 这是什么

Entity 的 `data` 字段是个 dict，里面的值符合什么类型、范围、必填，由本叶子的 **JSON Schema 驼峰风格** 规则定义。02-entity SPEC §5 已经预留 `Validator` 协议接口；本叶子提供**真校验器实施**。

业务白话：业务同事在 `02-mod/02-entities.md` 声明 entity_type=`entry` 的 schema：

```yaml
entry:
  title:
    type: string
    required: true
    minLength: 1
    maxLength: 200
  body:
    type: string
  tags:
    type: array
    items: {type: string}
  image:
    type: asset_ref           # 新增 ZE 字段类型
```

framework 启动时 `doc.add(Entity(type="entry", data={"title": ""}))` → 本叶子的 validator 校验 title 不能空字符串 → 抛 `ZeError("schema_violation")` → 业务方提示用户"标题不能为空"。

## 2. JSON Schema 驼峰风格

跟 v2 一致 + 跟 JSON Schema spec 兼容。**驼峰命名**（不是蛇形），便于跟前端 JSON 互通。

### 顶层 schema 结构

```python
{
    "type": "<type-name>",       # 必填 (或 oneOf/anyOf/allOf)
    # 类型特定约束 (见 §3)
}
```

framework validator 的唯一内部 Entity 形态是这个 canonical root object，或 root
`oneOf/anyOf/allOf` composite。历史 mod 可继续提交
`{"title": {"type": "string"}}` 这种 flat field-map，但它只在
`make_validator()` 工厂边界被确定性包成
`{"type": "object", "properties": <flat>}`；Document 中保存的声明原文不改，
因此不要求数据格式迁移。运行期其他位置不得再次猜测 flat/canonical。
根关键字按**值形状**识别：`type` 必须是 exact string，`properties` 必须是字段到
object-schema 的 exact map，`required` / composite 必须是 exact list；所以业务字段就算
名为 `required`、`oneOf`、`type` 或 `properties`，其值是字段 schema object 时仍按 flat
字段处理，而不会只因名字碰撞被误判成 root keyword。

### 嵌套约束（属于父字段）

```python
# 字段表 (在 entity schema 顶层)
{
    "type": "object",
    "properties": {
        "title": {"type": "string", "required": True},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title"],          # JSON Schema 标准: required 是父对象的字段列表
    "additionalProperties": false,
}
```

**兼容性**：每个字段的 schema 可以**单独**写 `required: True`（v2 风格 / mod 模板风格）—— 本叶子内部把它当作"父对象的 required 数组里加这个字段名"处理。详见 §5。

## 3. 类型清单

### 3.1 string

```python
{"type": "string",
 "minLength": int,
 "maxLength": int,
 "pattern": str,                 # 正则; 用 re.fullmatch
 "enum": list[str],              # 枚举值
 "format": "email" | "uri" | "uuid" | "iso_datetime"}
```

支持的 `format`：
- `email` — 简化正则 `[^@\s]+@[^@\s]+\.[^@\s]+`
- `uri` — `https?://[^\s]+`
- `uuid` — `[0-9a-fA-F-]{36}` 或 32 字符 hex（v3 内部用 uuid4 hex）
- `iso_datetime` — `YYYY-MM-DDTHH:MM:SS[.fff][Z|±HH:MM]`

### 3.2 number / integer

```python
{"type": "number" | "integer",
 "minimum": float,
 "maximum": float,
 "exclusiveMinimum": float,
 "exclusiveMaximum": float,
 "multipleOf": float}
```

- `integer` 拒绝 float；`number` 接受 int + float
- `bool` 在 Python 是 `int` 子类，但本叶子**不视为 number**（避免误判）

### 3.3 boolean

```python
{"type": "boolean"}
```

只接受 `True` / `False`，不接受 `0` / `1` / `"true"`。

### 3.4 array

```python
{"type": "array",
 "minItems": int,
 "maxItems": int,
 "uniqueItems": bool,
 "items": <schema>,              # 每项子 schema
 # 兼容 v2: "itemSchema" 也接受 (alias)
}
```

### 3.5 object

```python
{"type": "object",
 "properties": {key: <schema>, ...},
 "required": list[str],          # 父对象必填字段名
 "additionalProperties": bool}   # 默认 True
```

### 3.6 null

```python
{"type": "null"}
```

只接受 `None`。常跟 `oneOf` 组合做 nullable（见 §3.8）。

### 3.7 asset_ref（ZE 专有）

```python
{"type": "asset_ref"}
```

校验值形态：

```python
{
    "sha256": str,                # 必填; 64 hex 字符
    "mime": str,                  # 可选; 默认 "application/octet-stream"
    "size": int,                  # 可选; >= 0
    "ext": str,                   # 可选; 含或不含前导点都接受
}
```

**不校验**：blob 是否真存在（那是 03-asset 层的事）。本层只校 dict 结构。

### 3.8 entity_ref（ZE 专有）

```python
{"type": "entity_ref",
 "to": "<entity_type>"}            # 可选; 限制只能引用某 type
```

校验值形态：

```python
{"$ref": "<entity_type>/<entity_id>"}
```

**不校验**：被引 entity 是否真存在（那是 Document 层的事）。`to` 字段非空时限制引用类型必须匹配。

### 3.9 复合: oneOf / anyOf / allOf

```python
{"oneOf": [<schema>, <schema>, ...]}      # 必须命中且仅命中 1 个
{"anyOf": [<schema>, ...]}                 # 命中 >= 1 个
{"allOf": [<schema>, ...]}                 # 全部命中
```

**nullable 模式**：

```python
{"oneOf": [{"type": "string"}, {"type": "null"}]}
```

### 3.10 untyped（不指定 type）

`{}` 或 `{"required": True}` 视为"任意值"——什么都接受。供原型阶段用；生产应明确 type。

## 4. validate(value, schema, path) 入口

```python
def validate(value, schema, path="$") -> None
```

跟 02-entity SPEC §5 的 `Validator` 协议**签名一致**。不通过抛 `ZeError("schema_violation")` 含 path + 原因 + value。

`path` 用于错误定位：

- 顶层 entity.data: `path="$obj"`（entity 类型作前缀）
- 嵌套字段: `path="$obj.bbox[2]"`
- 复合: `path="$obj(oneOf[1])"`

## 5. `required` 两种风格的兼容

业务方写 schema 时，"必填"有两种风格：

**A. JSON Schema 标准** — required 在父对象上：

```python
{
  "type": "object",
  "properties": {"title": {"type": "string"}},
  "required": ["title"],
}
```

**B. v2 / mod 模板风格** — required 在字段上：

```python
{
  "type": "object",
  "properties": {"title": {"type": "string", "required": True}},
}
```

本叶子**两种都支持**。预处理：扫 `properties` 里每个字段的 `required: True`，等同于父对象 `required` 数组加上该字段名。

## 6. make_validator(entity_schema)

```python
def make_validator(entity_schema: dict) -> Validator
```

工厂函数 → 返回符合 02-entity Validator 协议的 callable：

```python
Validator = Callable[[dict, dict, str], None]
# (data, schema, path) -> None
```

返回的 callable 接收 `(data, schema, path)`：
- `data` 是 entity.data 整个 dict
- `schema` 为稳定 Validator 协议参数；framework wrapper 不允许它覆盖自己持有的规范化 authority（custom validator 仍可自行解释）
- `path` 是错误定位前缀

工厂不会返回普通闭包，而是返回一个私有 exact-type callable wrapper。公开能力仍然只有
`Validator` callable 协议；`is_framework_validator(value, expected_schema=None)` 是给可信
持久化边界使用的私有判定：省略 expected 时只接受 framework 工厂的精确 wrapper，不接受
custom callable 或其子类；传 expected 时还会走同一规范化规则核对 wrapper 实际绑定的
authority，错误 schema 生成的真 wrapper 也不算匹配。
wrapper 会用 Entity 叶的 strict JSON clone 隔离 `entity_schema`，并在工厂内把 legacy flat
field-map 单点规范成 canonical root object；canonical typed/composite 保持原语义，省略 type
但含 object keywords 的 schema 补成 object。调用方在工厂返回后修改原 schema（包括嵌套
list/dict），或在调用时传 raw/宽松 schema，都不会改变 wrapper 的校验语义。这个身份不是安全签名；
它只证明恢复器可以从同一份 Document schema 确定性重装 framework validator。

**主用法**（Document.add 时注入）：

```python
from field_types import make_validator

validator = make_validator(doc.schema[entity.type])
entity.install_validator(doc.schema[entity.type], validator)
# 注入后 entity 后续 update 时自动跑校验
```

## 7. 跟 Entity / Document 集成

### 7.1 Entity 端（已就位）

`Entity` 早就预留：
- `__init__(..., validator=...)` — 构造时可注入
- `_validator: Validator` — 内部存校验器
- 构造时 schema 非空就跑一次 `_validator(self._data, self._schema, f"${self._type}")`
- `update(patches, ..., validator=)` — 参数为签名兼容，只可省略或重复已安装对象；不能临时覆盖；非空 schema 对完整候选 data 只校验一次

**新增 install_validator helper**：

```python
def install_validator(self, schema: dict, validator: Validator) -> None:
    """Framework 内部用: 给已构造的 entity 注入 schema + validator.

    立即按当前 data 跑一次校验; 失败抛 ZeError("schema_violation").
    通常 Document.add(entity) 时调.
    """
    self._schema = schema
    self._validator = validator
    if self._schema:
        validator(self._data, self._schema, f"${self._type}")
```

### 7.2 Document 端（本 commit 改）

`doc.add(entity)` 自动按 type 接管 authority：

```python
def add(self, entity: Entity) -> Entity:
    # ... exact Entity / id 冲突检查 ...
    type_schema = self._schema.get(entity.type)
    if type_schema:
        entity.install_validator(type_schema, make_validator(type_schema))
    else:
        entity.install_validator({}, _no_op_validator)

    bucket[entity.id] = entity
    ...
```

**条件**：
- standalone Entity 可带 custom，但进入 Document 后必须被 Document authority 覆盖
- 非空 type schema 注入即校验完整当前 data；空/缺 schema 固定 exact no-op
- restore/bootstrap 使用 `attach_validator` 覆盖 authority 但不重验历史数据
- 后续 update 对完整候选 data 校验一次，不按 patch 切子 schema

### 7.3 业务方写法

业务方在 mod `register` 函数里声明 schema → Document 启动时灌进 `doc._schema` → 后续 `doc.add(entity)` 自动校验：

```python
def register(registry: DocumentRegistry) -> None:
    registry.declare_entity_type(
        name="entry",
        schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "required": True,
                          "minLength": 1, "maxLength": 200},
                "tags": {"type": "array", "items": {"type": "string"}},
                "image": {"type": "asset_ref"},
            },
        },
        collaboration_unit=True,
    )

# 业务 command 内:
doc.add(Entity(type="entry", data={"title": "Hello", "tags": ["a"]}))
# → validator 自动跑, OK 加入
# doc.add(Entity(type="entry", data={"title": ""}))
# → 抛 ZeError("schema_violation", "$entry.title: minLength 1")
```

## 8. Python API

```python
from field_types import validate, make_validator

# 直接校验
validate(value="hello", schema={"type": "string", "minLength": 1}, path="$x")  # ok
validate(value="", schema={"type": "string", "minLength": 1})                   # 抛 ZeError

# 工厂 (返 Validator callable)
v = make_validator({
    "type": "object",
    "properties": {
        "name": {"type": "string", "required": True},
        "image": {"type": "asset_ref"},
    },
})
v(data={"name": "x"}, schema={...}, path="$obj")   # 跑校验
```

## 9. 红线规则 + 反模式

红线：

- **本叶子是纯函数** — `validate` / `make_validator` 不读写任何外部状态（AP-FT-1）
- **不查 entity / asset 真存在性** — `asset_ref` 只校形态；`entity_ref` 只校 `$ref` 形态；真存在性由 Document / AssetStore 层
- **bool 不是 number** — Python `isinstance(True, int)=True` 是坑；本叶子明确拒绝

反模式：

- **AP-FT-1** validator 内调外部 API / 改 entity → 应该纯
- **AP-FT-2** schema 用 `{type: "asset_ref"}` 但 data 塞 binary → 走 03-asset 不应到本叶子（业务方误用）
- **AP-FT-3** 同字段 schema 写 enum + pattern + format 三个互相矛盾的 → 验证逻辑不确定；建议挑一种
- **AP-FT-4** required 既在父对象 `required` 数组又在字段上 `required: True` → 重复但不矛盾，本叶子合并去重
- **AP-FT-5** 把 computed / crdt 类型直接写在 schema → 这俩是声明式 metadata，引擎不在本叶子（v2 declare_computed / declare_crdt 等 04-history-rollback / 03-offline-first）
- **AP-FT-6** 大量正则 pattern 没缓存 → 性能差；caller 可以预编译

## 10. 跟 audit gate 关系

| Gate | 验证内容 | 形态 |
|---|---|---|
| `GATE-FT-01` | string type 拒非 str | runtime |
| `GATE-FT-02` | required 风格双兼容（A + B 都通过）| runtime |
| `GATE-FT-03` | asset_ref 形态校验 | runtime |
| `GATE-FT-04` | entity_ref 形态校验 + to 限制 | runtime |
| `GATE-FT-05` | nullable (oneOf null) | runtime |
| `GATE-FT-06` | bool 不视为 number | runtime |

加进 [_protocol/AUDIT-GATES.md](../../_protocol/AUDIT-GATES.md) 注册表。

## 11. 当前实装状态

### ✅ 已实装

| 项 | 说明 |
|---|---|
| `validate(value, schema, path)` | 跟 v3 Validator 协议签名一致 |
| 6 基础类型 | string / number / integer / boolean / array / object |
| string 约束 | minLength / maxLength / pattern / enum / format |
| number 约束 | minimum / maximum / exclusiveMin/Max / multipleOf |
| array 约束 | minItems / maxItems / uniqueItems / items / itemSchema (alias) |
| object 约束 | properties / required / additionalProperties |
| 复合 | oneOf / anyOf / allOf |
| `null` 类型 | 配合 oneOf 做 nullable |
| **`asset_ref`** | ZE 专有: 校 sha256 + mime + size + ext 形态 |
| **`entity_ref`** | ZE 专有: 校 `{"$ref": "type/id"}` 形态 + 可选 `to` 限制 |
| `untyped` | 任意值 (原型阶段) |
| `required` 双风格 | A (父对象 required 数组) / B (字段上 required: True) 都接受 |
| `make_validator(schema)` | 返回持有 canonical root authority 的 exact callable；legacy flat field-map 仅在此单点规范化，caller schema 不可覆盖 |
| `Entity.install_validator(schema, validator)` | Framework 内部用; 注入即校验 |
| `Document.add(entity)` authority | 只接受 exact Entity；覆盖 standalone custom；非空 schema 立即校验，空/缺 schema exact no-op |
| 跟 `_protocol/audit_gates.py` 集成 | 加 6 个 GATE-FT-* |

### ⏳ 待补（真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| `computed` 字段（声明 + 引擎）| `02-data-flow/04-history-rollback` 字段历史驱动重计算 |
| `crdt` 字段（声明 + 引擎）| `02-data-flow/03-offline-first` CRDT 引擎 |
| 跨 Doc reference 解析 | 不支持 (v3 单 Doc 约定; SPEC §1 已说明) |

### 原则

- **纯函数**：无副作用 / 可并发调
- **不查存在性**：本叶子只校形态；真存在性归 Document / AssetStore
- **JSON Schema 驼峰**：跟 v2 + JSON Schema 生态兼容
- **签名先定**：`asset_ref` / `entity_ref` 类型签名固定；上层叶子完成后行为可扩展
