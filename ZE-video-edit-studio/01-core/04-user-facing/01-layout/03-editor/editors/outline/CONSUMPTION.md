# Outline editor consumption rules

## Layout 声明

```ts
slot('tree', 'outline', ['scene_node'], {
  parentField: 'parentId',   // 父节点 id 字段(空/缺/悬挂 = 根)
  orderField: 'order',       // 同层排序(number)
  labelField: 'title',       // 可选:行标题;缺省 entity.name
  iconField: 'icon',         // 可选:节点图标(图库名)
  thumbField: 'assetRef',    // 可选:行缩略图(asset_ref)
  badgeField: 'role',        // 可选:行 badge
  visibilityField: 'visible',
  lockedField: 'locked',
  renameField: 'label',
  duplicateCommand: 'node.duplicate',
})
```

## `view_state` 消费规则

- `parentField` / `orderField`:父子与排序字段映射;缺省 `parentId` / `order`。
- `labelField`:可选,行标题字段;缺省 `entity.name`。
- `iconField`:可选,值为图库图标名(`icons.md` 名册);无则不渲图标。
- `thumbField`:可选,值为 `asset_ref`;用于图层树缩略图。
- `badgeField`:可选,值为 string/number/bool;用于 role/status 等轻量标记。
- `visibilityField` / `lockedField`:可选 boolean 字段;行内眼睛/锁用内置 `entity.update` CAS 切换。`visible` 缺省 true,`locked` 缺省 false。
- `renameField`:可选,双击标题重命名;`name` / `__name` 写信封名,其它值写 data 字段。
- `deleteCommand`:可选,缺省 `entity.delete`。
- `duplicateCommand`:可选,声明后渲复制按钮并 dispatch `{type,id,expected_version}`。
- `actions`:可选 `ToolbarAction[kind=command]`,渲进控制区;空则不渲控制区。
- 选择:复用统一 `selectionModel`(点击=replace、⌘=toggle、⇧=range、⌘A=全选可见、⌘⇧A=反选可见、Esc=清空)。展开/折叠是**本地视图态**,不进 view_state、不进 Operation。

## 组合逻辑

层级 + 详情用 `outline` + `form`:outline 只负责选中哪个节点和行级通用操作,form 负责改该节点字段。2D 图层树 / 3D 节点树 / 文档目录都是同一 `outline` 类的实例(实体集不同、parent/order 形状相同)。

节点集扁平(无 `parentField`)→ 改用 `gallery` compact-list,不要硬套 outline。
flat 图层列表可用 `gallery`;tree/group 图层用 `outline`。canvas2d 只负责画布投影,不拥有图层列表。

## 写入规则

所有写入经 `dispatch(commandId, payload)` 带 `type/id/expected_version`。拖拽改父用 `lib/treeModel.computeReparent` 算出 `{id,parentId,order,expected_version}[]`,作为**单 Operation 批量命令**(`entity.batch_update` 形态)派发,保证一次 ⌘Z 撤销整次移动、CAS 一致。锁定节点不可拖拽 reparent;其它节点仍可 drop 到它的 before/inside/after 区。
