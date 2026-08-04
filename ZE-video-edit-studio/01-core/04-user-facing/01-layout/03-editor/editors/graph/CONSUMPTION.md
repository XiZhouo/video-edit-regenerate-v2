# Graph editor consumption rules

## Layout 声明

```ts
slot('flow', 'graph', ['node'], { xField: 'x', yField: 'y', labelField: 'title', depsField: 'deps', addCommand: 'node.add' })
```

## `view_state` 消费规则

- xField/yField: 节点坐标字段
- labelField: 节点标题字段
- depsField: 依赖边字段
- addCommand/updateCommand/deleteCommand: 增改删命令

## 组合逻辑

graph 做关系总览；选中节点后用 form 改节点参数，用 review/table 看节点产物。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
