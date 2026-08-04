# Conflict Resolution — 数据层冲突解决

## 1. 这是什么

并发请求被 [05-concurrency/](../05-concurrency/) 串行化后，**数据层面仍可能逻辑冲突**：A 改了字段 X，B 也改了 X，base 是 v=3，谁赢？怎么 merge？

本叶子提供**4 种冲突解决策略**给 `Command` 选用。每个 `CommandSpec` 在 `conflict_strategy` 字段声明用哪种；冲突发生时框架调本叶子的 `resolve(...)` 返回最终值（或抛错让 caller 决定）。

业务白话：业务同事 A 和 B 都把同一条知识条目的标题改了。A 改成 "X"，B 改成 "Y"，base 都是 "原标题"。`conflict_strategy`：
- `reject` → 谁先到谁赢，后到的拿到 cas_version_mismatch，自己决定怎么办
- `last-write-wins` → 后到的覆盖，A 的改没了
- `three-way-merge` → 两个都改了不同值 → 抛错（A B 都没赢）
- `crdt` → 用 CRDT 数据类型自动合并（⏳ 等 03-offline-first）

### 跟 05-concurrency 的边界

| | 05-concurrency | 02-conflict-resolution (本叶子) |
|---|---|---|
| 关注 | 访问层（不打架）| 数据层（结果不打架）|
| 时机 | 请求 dispatch 前 | 数据 merge 阶段 |
| 工具 | Lock / Queue / RateLimiter | 4 策略 resolver |

调用顺序：先 concurrency 让请求按序到，**串行化后仍冲突**才走本叶子。

### 跟 CAS 的关系

`entity.update(patches, expected_version=N)` 是**入口级硬约束**——版本不匹配直接抛 `cas_version_mismatch`，这是默认的 `reject` 策略实施层。如果 caller 想用别的策略（LWW / 3-way / CRDT），不能直接调 entity.update（它只支持 reject）；要走：

```
caller 拿到 entity 当前版本
  ↓
caller 调本叶子 resolve(strategy=..., base, current, incoming) → resolved
  ↓
caller 用 resolved 值调 entity.update(patches, expected_version=current_v)
```

本叶子不替代 CAS，**它是 caller 在 CAS 之上的决策层**。

## 2. 4 种策略

```python
def resolve(
    *, strategy: str,
    base: Any,         # 双方分叉时的共同祖先值
    current: Any,      # store 当前值 (别人 update 过的)
    incoming: Any,     # 你要写入的新值
    crdt_merge: Callable | None = None,    # crdt 策略可选
) -> Any:
    ...
```

### 2.1 reject（默认）

最严格：**只要 current != base 就抛错**，让 caller 自己 retry / merge / 放弃。

```python
resolve(strategy="reject", base="原", current="A的修改", incoming="B的修改")
# 抛 ZeError("cas_version_mismatch")
```

适合：金额 / 关键配置 / 任何"宁愿失败也不能错"的场景。

### 2.2 last-write-wins (LWW)

直接 `return incoming`，不管 current 是啥。

```python
resolve(strategy="last-write-wins", base="原", current="A的修改", incoming="B的修改")
# return "B的修改"  (A 的改没了)
```

适合：实时光标位置 / 用户偏好（无关紧要的字段）/ 频繁同步无需 merge 的场景。

**注意**：LWW 会**默默丢失 A 的修改**。审计层（OpLog）仍能查到 A 的 Operation 存在，但当前数据状态里没了。

### 2.3 three-way-merge

经典 git 风格 3-way 合并：

| base 跟 current 关系 | base 跟 incoming 关系 | 结果 |
|---|---|---|
| current == base | incoming == base | base (双方都没改) |
| current != base | incoming == base | current (只 A 改了) |
| current == base | incoming != base | incoming (只 B 改了) |
| current != base, incoming != base, current == incoming | | current (碰巧改成一样) |
| current != base, incoming != base, current != incoming | | 抛 `cas_version_mismatch` (都改且不同) |

```python
resolve(strategy="three-way-merge", base="原", current="A的修改", incoming="原")
# return "A的修改"  (只 A 改了, B 没改)

resolve(strategy="three-way-merge", base="原", current="A", incoming="B")
# 抛 ZeError (都改但不一样)
```

适合：协作编辑器但不要 CRDT 那么重 / 大部分情况只一边改的场景。

### 2.4 crdt（⏳ 占位）

用 CRDT 数据类型自动合并（LWW-Register / G-Counter / OR-Set / RGA 等）。**当前抛 NotImplementedError**——CRDT 引擎是 412 行的大件，跟 [03-offline-first/](../03-offline-first/) 强耦合，等那个叶子启动一起做。

```python
resolve(strategy="crdt", base=..., current=..., incoming=...)
# 抛 NotImplementedError("CRDT 策略待补; 等 03-offline-first 叶子启动")
```

设计已经预留 `crdt_merge: Callable | None = None` 参数——caller 临时需要时可以传自定义 merge 函数：

```python
def my_merge(current, incoming):
    return current | incoming   # set union 例子

resolve(strategy="crdt", base=..., current=..., incoming=..., crdt_merge=my_merge)
# return my_merge(current, incoming)
```

## 3. resolve() 完整签名

```python
def resolve(
    *,
    strategy: str,                                # "reject" / "last-write-wins" / "three-way-merge" / "crdt"
    base: Any,
    current: Any,
    incoming: Any,
    crdt_merge: Callable[[Any, Any], Any] | None = None,
) -> Any
```

**返回**：解决后的值；caller 拿这个值去 `entity.update(...)`。

**抛错**：
- `cas_version_mismatch` — reject 策略 current != base / 3-way 双方冲突
- `validation_failed` — 未知 strategy 字符串
- `NotImplementedError` — strategy="crdt" 但没传 `crdt_merge`

**纯函数**：本叶子不读写 entity / doc / store，纯接收三个值 → 返回一个值。这是协议层。

## 4. 跟 Command.conflict_strategy 字段对应

每个 `CommandSpec` 的 `conflict_strategy` 字段（默认 `"reject"`）声明用哪种策略：

```python
@registry.command(
    id="entry.update-title",
    label="改标题",
    kind="native",
    touches=["entry"],
    conflict_strategy="three-way-merge",     # 这里
)
def update_title(args, actor, source, doc):
    ...
```

**Command 层不强制走 resolver**——业务方在 handler 里决定要不要查 base / current / incoming + 调 resolve。framework 不自动调。

为什么不自动：
- caller 才知道哪个字段是 base / current / incoming
- 不同 command 的语义差异大，框架硬接路径会越界
- `conflict_strategy` 字段是**声明**，便于审计和工具检查；运行时由业务方按需调

## 5. 4 策略选型指引

| 业务场景 | 推荐策略 | 理由 |
|---|---|---|
| 金额 / 财务 / 关键配置 | `reject` | 宁可失败不能错 |
| 实时光标位置 / 用户偏好 | `last-write-wins` | 频繁同步, 旧值过时即可丢弃 |
| 标题 / 描述等 metadata（多数没冲突）| `three-way-merge` | 只一边改时自动合, 双方改才抛 |
| 协作编辑富文本 / 共享列表 | `crdt`（待补） | 真正的多人 merge |
| 简单字段 + 高吞吐 | `last-write-wins` | 性能最好 |
| 复杂结构 + 严格一致性 | `reject` | 让上层应用决定 retry / merge |

## 6. Python API

```python
from resolver import resolve

# reject (默认)
resolve(strategy="reject", base="X", current="X", incoming="Y")     # → "Y"
resolve(strategy="reject", base="X", current="A", incoming="Y")     # 抛 ZeError

# last-write-wins
resolve(strategy="last-write-wins", base="X", current="A", incoming="Y")  # → "Y"

# three-way-merge
resolve(strategy="three-way-merge", base="X", current="X", incoming="Y")  # → "Y"
resolve(strategy="three-way-merge", base="X", current="A", incoming="X")  # → "A"
resolve(strategy="three-way-merge", base="X", current="A", incoming="B")  # 抛 ZeError

# crdt (custom merge fn)
resolve(strategy="crdt", base={"a"}, current={"a", "b"}, incoming={"a", "c"},
        crdt_merge=lambda c, i: c | i)                              # → {"a", "b", "c"}

# 未知策略
resolve(strategy="unknown", base=..., current=..., incoming=...)    # 抛 ZeError
```

## 7. 复合场景：嵌套 / 列表 / dict

本叶子的 4 策略**对单值生效**——`base / current / incoming` 通常是字段级别值（string / number / bool / 小 dict）。

复合数据怎么办：
- **dict 整体替换**：把 dict 作为一个 value 传入 resolve；用 `==` 比较；适合 schema 配置类
- **dict 字段级 merge**：caller 自己遍历 keys，对每个 key 调 resolve；适合 metadata
- **list**：本叶子不内置 list merge；需要时走 CRDT 的 RGA / OR-Set（待补）
- **嵌套对象**：递归调 resolve；caller 责任

```python
# 字段级 merge 示例
merged = {}
for k in base.keys() | current.keys() | incoming.keys():
    merged[k] = resolve(
        strategy="three-way-merge",
        base=base.get(k), current=current.get(k), incoming=incoming.get(k),
    )
```

## 8. 跟 OpLog 集成

本叶子**不直接写 OpLog**。Command handler 内部调 resolve 后：
- 如果 resolve 返回 resolved 值 → 用 resolved 调 `entity.update(...)` → 产生 Operation 落 OpLog
- 如果 resolve 抛 `cas_version_mismatch` → caller 决定 retry / 回报错给 UI（不产生 Operation 也合理）

审计需求："谁 merge 了 conflict" 由 caller 在 handler 里记 ai_provenance / actor_log（actor.source 可以标 "system" / "user"）。

## 9. Python 实现简洁度

resolve 是个**60 行内的纯函数**（不含 crdt 占位）。本叶子代码量很小但**语义浓度高**——4 种策略是 ZE 协作框架的核心约定。配套测试 ~200 行覆盖 4 策略 happy / sad / 边界。

## 10. 红线规则 + 反模式

红线：

- **resolve 必须是纯函数** — 不读写 entity / doc / 全局状态（AP-R1）
- **不能绕过 CAS 直接用 LWW** — `entity.update` 的 expected_version 不能省；LWW 是上层选项，不替代版本号校验（AP-R2）
- **strategy 字符串只 4 个值** — 不接受变体（"lww" / "last_write_wins" / "lww" 等同名），统一用 SPEC §2 列出的 4 个全名
- **crdt 没 crdt_merge 抛 NotImplementedError** — 当前不假装实装（AP-R3）

反模式：

- **AP-R1** resolver 里读 doc / 调 entity API → 应纯函数；要查数据 caller 在外面查好再传入
- **AP-R2** 用 LWW 当万能解 → "永远不报错" 的代价是 "永远不知道丢了什么"；金额类必须用 reject
- **AP-R3** 自己 silent-implement CRDT 简化版 → 假装实装比留 NotImplementedError 更糟；真要做去 03-offline-first
- **AP-R4** 把 strategy 字段当"实施层" → strategy 只是声明; 真调 resolve 由 caller 决定时机
- **AP-R5** 复合 dict / list 整体走 reject → 任何嵌套字段变化都失败; 应字段级 merge 或 CRDT
- **AP-R6** 3-way merge 双方改但碰巧值相同, 当作 "无冲突" → SPEC 已规定 value 相同视为 "都改成一样"，取 current（不是冲突）

## 11. 当前实装状态

### ✅ 已实装

| 项 | 说明 |
|---|---|
| `resolve(strategy, base, current, incoming, crdt_merge=None)` | 纯函数; 4 策略路由 |
| `reject` 策略 | current != base 抛 cas_version_mismatch |
| `last-write-wins` 策略 | 直接返 incoming |
| `three-way-merge` 策略 | 经典 git 风格 5 分支判断 |
| `crdt` 策略 (custom merge) | caller 传 crdt_merge fn 就用; 否则抛 NotImplementedError |
| 未知 strategy 抛 validation_failed | 防业务方拼错 |
| ZeError 共享 | 从 entity 模块 import |
| 别名兼容 | `last_write_wins` / `three_way_merge` (下划线变体) 也接受 |

### ⏳ 待补（真有跨叶子依赖）

| 项 | 依赖 |
|---|---|
| CRDT 引擎（LWW-Register / G-Counter / OR-Set / RGA / Causal Tree）| `03-offline-first` 启动后实装 |
| Vector Clock | `01-realtime-sync` + `03-offline-first` |
| 自动 conflict detection（CAS hook）| `01-command` 的 dispatch 集成（当前 framework 不自动调 resolve）|
| 复合 dict / list 字段级 merge helper | 待业务真实场景驱动；当前 caller 自己遍历 |
| conflict 日志（哪些 op 经历了 merge / reject）| `02-operation` 的 OpLog 已能覆盖（Operation.command_id 体现策略选择）|

### 原则

- **resolve 是纯函数** — 输入 → 输出，无副作用；可重入 / 可并发调
- **协议层不实施层** — 本叶子定 4 策略的语义；实际何时调 resolve 由 caller 决定
- **CAS 是底层硬约束** — 本叶子是上层决策；不替代版本号校验
