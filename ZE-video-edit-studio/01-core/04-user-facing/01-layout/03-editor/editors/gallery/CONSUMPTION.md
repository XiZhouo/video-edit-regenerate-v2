# CardList editor consumption rules — 卡片编辑器（原缩略图编辑器）

## Layout 声明

```ts
slot('layers', 'cardlist', ['layer'], {
  layoutMode: 'compact-list',
  cardSize: 'compact',
  thumbField: 'assetRef',
  visibilityField: 'visible',
  lockedField: 'locked',
  searchFields: ['name', 'kind'],
  sorts: [
    { id: 'orderAsc', labelKey: 'mod.sort.orderAsc', field: 'order', valueType: 'number', direction: 'asc' },
    { id: 'nameAsc', labelKey: 'mod.sort.nameAsc', field: 'name', valueType: 'natural', direction: 'asc' },
  ],
  defaultSort: 'orderAsc',
  reorderCommand: 'layer.reorder',
})
```

`gallery` 仍是兼容 alias,新布局不要继续使用。

## `view_state` 消费规则

- `thumbField` / `labelField` / `descriptionField` / `summaryFields`:卡片内容字段。
- `layoutMode`: `grid` / `card-list` / `compact-list`;flat 图层列表通常用 `compact-list`。
- `cardSize`: `compact` / `balanced` / `spacious` 卡片尺寸默认 preset。用户可用 ManageZone 的“缩略图尺寸”无级滑杆覆盖为本地项目偏好;不进 `view_state`。
- `searchFields` / `filters` / `sorts` / `defaultSort`:进入 ManageZone;只改变当前可见集,不 dispatch。
- `selectionActions`:全选、反选、清空、删除、批量动作。
- `actions` / `adoptCommand` / `deleteCommand` / `importCommand`:进入 ToolZone。
- `visibilityField` / `lockedField` / `reorderCommand`:flat 图层列表消费规则。

## 组合逻辑

做 2D 图层工作台时,cardlist 承接 flat 图层列表;tree/group 图层列表使用 outline。canvas2d 是画布,property 是属性。它们只通过 `selection[layer]` 同步。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`,也仍然要带 `type/id/expected_version`,让 undo、CAS、运行态和审计链路保持一致。
