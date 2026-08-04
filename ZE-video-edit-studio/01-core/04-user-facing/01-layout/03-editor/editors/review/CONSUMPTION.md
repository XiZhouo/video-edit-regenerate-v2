# Review editor consumption rules

## Layout 声明

```ts
slot('review', 'review', ['candidate'], { statusField: 'decision', noteField: 'note', beforeField: 'sourceRef', afterField: 'resultRef', compareMode: 'slider' })
```

## `view_state` 消费规则

- statusField: 审阅状态字段，缺省 status
- noteField: 批注字段，缺省 note
- beforeField/afterField/compareMode: 可选前后图对比
- showDecision: false 时隐藏决策和批注，只留对比

## 组合逻辑

gallery/table 负责选候选，review 负责给当前候选下决定；需要详情字段再配 form。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
