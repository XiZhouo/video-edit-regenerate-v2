# Compare editor consumption rules

## Layout 声明

```ts
slot('diff', 'compare', ['result'], { beforeField: 'sourceRef', afterField: 'outputRef', mode: 'slider' })
```

## `view_state` 消费规则

- beforeField/afterField: 两个 asset_ref 字段
- mode 或 compareMode: slider/side-by-side/onion

## 组合逻辑

compare 是 read-only 结果检查面板；要做判断时改用 review 或把 review 放在旁边。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
