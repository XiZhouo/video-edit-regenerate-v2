# Chart editor consumption rules

## Layout 声明

```ts
slot('metrics', 'chart', ['sample'], { chartKind: 'bar', xField: 'label', yField: 'score', labelField: 'name' })
```

## `view_state` 消费规则

- chartKind: bar/line/area/scatter/histogram/pie 等
- xField/yField/labelField/metricField: 字段映射；yField 优先于 metricField
- statusOf: 高亮运行态，但不改变数据

## 组合逻辑

chart 做概览，table/gallery 做明细，form/review 做单条解释。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
