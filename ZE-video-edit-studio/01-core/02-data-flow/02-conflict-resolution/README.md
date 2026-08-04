# 02-conflict-resolution — 冲突解决

> 一句话：**数据层 merge 策略**。4 个纯函数策略（reject / last-write-wins / three-way-merge / crdt），处理"并发改同一字段"产生的逻辑冲突。访问层串行化由 [05-concurrency/](../05-concurrency/) 管。

## 在框架里的位置

- 上一级：[02-data-flow/](../) — 数据流 5 个叶子
- 同级：[01-realtime-sync/](../01-realtime-sync/) / [03-offline-first/](../03-offline-first/) / [04-history-rollback/](../04-history-rollback/) / [05-concurrency/](../05-concurrency/)
- 跟 CAS 关系：本叶子是 caller 在 CAS 之上的决策层；不替代 `entity.update` 的 expected_version 硬约束

## 本叶子文件

| 文件 | 用途 |
|---|---|
| [README.md](./README.md) | 本文件（薄导览）|
| [SPEC.md](./SPEC.md) | 11 节完整规则（4 策略语义 / 选型指引 / 反模式）|
| [resolver.py](./resolver.py) | `resolve(...)` 纯函数 + `ALL_STRATEGIES` 常量 |
| [test_resolver.py](./test_resolver.py) | pytest 测试（23 个）|

## 当前状态

**✅ 已实装**（全部无跨叶子依赖项）

- `resolve(strategy, base, current, incoming, crdt_merge=None)` — 纯函数
- 4 策略：
  - `reject` — current != base 抛 `cas_version_mismatch`
  - `last-write-wins` — 直接返 incoming
  - `three-way-merge` — 经典 git 风格 5 分支判断
  - `crdt` — caller 传 `crdt_merge` fn 就用；否则抛 `NotImplementedError`
- 别名兼容：`last_write_wins` / `three_way_merge`（下划线变体）
- 未知 strategy 抛 `validation_failed`
- `ALL_STRATEGIES` 常量
- 纯函数：不读写 entity / doc，无副作用，可重入

**⏳ 待补**（真有跨叶子依赖）

- 内置 CRDT 引擎（LWW-Register / G-Counter / OR-Set / RGA）— 等 [03-offline-first/](../03-offline-first/) 启动
- Vector Clock — 等 [01-realtime-sync/](../01-realtime-sync/) + 03-offline-first
- 自动 conflict detection hook 接进 dispatch — `01-command` 当前不自动调；business 自己决定时机
- 复合 dict / list 字段级 merge helper — 当前 caller 自己遍历

详见 [SPEC.md §11](./SPEC.md#11-当前实装状态)。

## 怎么用

```python
from resolver import resolve, ALL_STRATEGIES

# reject (默认严格)
resolve(strategy="reject", base="X", current="X", incoming="Y")
# → "Y"
resolve(strategy="reject", base="X", current="A", incoming="Y")
# 抛 ZeError("cas_version_mismatch")

# last-write-wins (后到的覆盖)
resolve(strategy="last-write-wins", base="X", current="A", incoming="Y")
# → "Y"  (A 的改没了)

# three-way-merge (git 风格)
resolve(strategy="three-way-merge", base="X", current="X", incoming="Y")
# → "Y"  (只 B 改, A 没动)
resolve(strategy="three-way-merge", base="X", current="A", incoming="X")
# → "A"  (只 A 改, B 没动)
resolve(strategy="three-way-merge", base="X", current="A", incoming="B")
# 抛 ZeError  (双方都改且不同)

# crdt (custom merge)
resolve(strategy="crdt",
        base=set(), current={"a", "b"}, incoming={"a", "c"},
        crdt_merge=lambda c, i: c | i)
# → {"a", "b", "c"}
```

## 4 策略选型指引

| 业务场景 | 推荐策略 |
|---|---|
| 金额 / 财务 / 关键配置 | `reject` |
| 实时光标位置 / 用户偏好 | `last-write-wins` |
| 标题 / 描述等多数无冲突 | `three-way-merge` |
| 协作编辑富文本 / 共享列表 | `crdt`（待补，目前用 custom `crdt_merge`） |

## 设计要点

- **纯函数** — 不读写 entity / doc；输入 → 输出
- **协议层不实施层** — SPEC 定 4 策略语义；framework **不自动**调 resolve（business 在 command handler 里按需调）
- **CAS 是底层硬约束** — 本叶子是上层决策；不替代 `entity.update` 的 expected_version
- **复合 value 用 ==** — dict / list 整体比较；字段级 merge 由 caller 遍历

## 跟 Command 的关系

`CommandSpec.conflict_strategy` 字段（默认 `"reject"`）声明用哪种策略——但**framework 不强制走 resolver**。Business 在 command handler 里决定：

```python
@registry.command(id="entry.update-title", touches=["entry"],
                  conflict_strategy="three-way-merge")
def update_title(args, actor, source, doc):
    e = doc.get("entry", args["id"])
    base = args["base_title"]               # caller 告诉我分叉时的原值
    current = e.data.get("title")
    incoming = args["new_title"]

    resolved = resolve(strategy="three-way-merge",
                       base=base, current=current, incoming=incoming)

    e.update({"title": resolved}, expected_version=e.version, actor=actor)
    return new_operation(...)
```

`conflict_strategy` 是**声明**便于审计 / 工具检查；运行时由 business 按需调。

## 跑测试

```bash
cd 01-core/02-data-flow/02-conflict-resolution/
python -m pytest test_resolver.py -v
```
