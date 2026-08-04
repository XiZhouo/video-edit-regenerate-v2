# Property editor (`property`, implemented by legacy `form`)

代码入口: `FormEditor.tsx`。目录名和组件名保留 `form` 是实现兼容层;公共 editor class 使用 `property`。

## 说明

Property 不是普通表单。它是基于 selection / entity range 的属性编辑器和信息操作台:选中一个 entity 时展示它的字段、只读信息、引用关系和动作;选中多个同型 entity 时进入聚合编辑;跨类型工作台可通过 `followSelection` 跟随最近选中。

`form` 这个词只表示低级字段/记录表单能力。任何新文档、新布局、新 manifest 消费都应写 `property`。

## Core 负责

- 按 schema 渲染字段、同值/混合值聚合显示、字段提交。
- `actions` / `sections` / `actionsByType` / `sectionsByType` 的 ToolZone 与 InfoControlUnit。
- `followSelection` 下跨类型最近选中的解析结果展示。
- 运行态、只读态、缺字段置灰、confirm、busy 单飞。

## Core 不负责

- 主编辑器内部的侧栏。属性面板不是 canvas/table/cardlist 的孩子。
- 业务动作的执行逻辑;按钮只 dispatch command。
- 把所有 UI 都塞成“表单”。只读信息、运行状态、引用关系、控制按钮应按 InfoControlUnit 分区。

## 消费入口

```ts
slot('props', 'property', ['box', 'shot'], {
  followSelection: true,
  sectionsByType: {
    box: [{ id: 'basic', kind: 'fields', title: '基础', fields: ['name', 'x', 'y', 'w', 'h'] }],
  },
  actionsByType: {
    box: [{ command: 'entity.delete', label: '删除', variant: 'danger', confirm: '删除?' }],
  },
})
```

稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
