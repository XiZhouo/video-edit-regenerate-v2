# Property editor consumption rules

## Layout 声明

```ts
slot('props', 'property', ['box', 'shot'], {
  followSelection: true,
  actionsByType: {
    box: [{ command: 'entity.delete', label: '删除', variant: 'danger' }],
  },
})
```

`form` 仍是兼容 alias 和低级实现名,新布局不要把它写成属性面板。

## `view_state` 消费规则

- `followSelection`:true 时跟随 `lastSelected`,并在绑定类型闭集里选目标。
- `targetTypes`:可声明 property 允许跟随的类型范围。
- `actions` / `actionsByType`:ToolZone 动作;`requiresFields` 缺字段时置灰。
- `sections` / `sectionsByType`:InfoControlUnit 声明;声明 sections 时顶部 actions 被忽略。
- `sections.kind`: `fields` / `readonly-kv` / `params` / `preview` / `variants`。
- `variants`:当前实体的多个副本/候选选择条,可内嵌在属性编辑器里,不另起 cardlist editor。

## 组合逻辑

任何 master editor(table/cardlist/task/canvas2d/timeline/outline) 的详情都用独立 property slot;通过 selection 联动,不通过组件嵌套。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。批量更新必须是一条 Operation,让 undo、CAS、运行态和审计链路保持一致。
