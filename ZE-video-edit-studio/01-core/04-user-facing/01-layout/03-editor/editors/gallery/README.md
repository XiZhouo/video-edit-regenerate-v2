# CardList editor (`cardlist`, legacy `gallery`) — 卡片编辑器（原缩略图编辑器）

代码入口: `GalleryEditor.tsx`。目录名保留 `gallery` 是兼容层;公共 editor class 使用 `cardlist`。

## 说明

CardList 是通用卡片容器编辑器。每张卡代表一个 entity,可以有图也可以没图;卡片有固定槽位:thumb、label、description、meta/summary、status、toggles、actions。它可以是素材池、候选列表、flat 图层列表、对象清单或知识卡列表;本质都是“选择并操作一组实体”。

每行多少个由容器宽度 + 当前缩略图尺寸连续 scale 自动决定。`cardSize` 仍是 mod 给出的默认 preset,用户可在 ManageZone 用“缩略图尺寸”无级滑杆覆盖为本地项目偏好:

- `compact`:图层列表、窄栏、短对象清单。
- `balanced`:候选池、资产池、普通结果列表。
- `spacious`:需要看清图像细节的大卡。

## Core 负责

- 缩略图/标题/描述/摘要字段映射,有图和无图都保持稳定 footprint。
- ManageZone:缩略图尺寸、搜索、筛选、排序、选择管理。
- ToolZone:采用、删除、导入、批量动作等 command 入口。
- 单选、多选、加选、减选、全选、反选、清空、删除选中。
- `visibilityField` / `lockedField` 的图层式眼睛与锁展示。

## Core 不负责

- 图层像素怎么合成、候选怎么生成、素材导入后的业务归档。
- 属性编辑;图层属性由独立 `property` editor 改。
- 复杂树状层级;用 `outline` editor。

## 消费入口

mod 不直接把这个组件塞进业务页面;mod 在 LayoutTree 里声明:

```ts
slot('layers', 'cardlist', ['layer'], {
  layoutMode: 'compact-list',
  cardSize: 'compact',
  thumbField: 'assetRef',
  labelField: 'name',
  searchFields: ['name', 'kind'],
  sorts: [
    { id: 'orderAsc', labelKey: 'mod.sort.orderAsc', field: 'order', valueType: 'number', direction: 'asc' },
    { id: 'nameAsc', labelKey: 'mod.sort.nameAsc', field: 'name', valueType: 'natural', direction: 'asc' },
  ],
  defaultSort: 'orderAsc',
  visibilityField: 'visible',
  lockedField: 'locked',
  reorderCommand: 'layer.reorder',
})
```

稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
