# Document editor consumption rules

## Layout 声明

```ts
slot('doc', 'document', ['block'], {
  blockTypeField: 'blockType',  // 块类型字段(开放词表:paragraph/heading/divider/…)
  textField: 'text',            // 块文本字段(markdown 行内源)
  parentField: 'parent',        // 父块 id(空/缺/悬挂 = 根块)
  orderField: 'order',          // 同层排序(number)
  mediaField: 'media',          // 可选:figure 块的资产字段(M1)
})
```

## `view_state` 消费规则

- `blockTypeField` / `textField` / `parentField` / `orderField` / `mediaField`:字段映射;缺省见 `block` archetype binding(blockType/text/parent/order/media)。
- 块类型是**编辑器拥有的开放词表**(`blockRegistry`),库存 `string` 不闭枚举;M0/M1 渲染 paragraph(行内 md)/heading(level)/divider/**markdown(整段块级 md:标题/列表/任务清单/引用/围栏码/段落都渲,适合百科一节=一块的粗粒度)**,未知类型回退按段落渲文本。
- 新增块走框架 `onAdd` 接缝(slotAdapters 接 `ctx.onAddFor` → `newEntityDefaults[block]`);mod 应在 `04-layout.config.ts` 的 `newEntityDefaults` 声明默认块(如 `{blockType:'paragraph', text:'', order:0}`)。
- 选择复用统一 `selectionModel`(点击=replace、⌘=toggle、⇧=range);进编辑 = 本地视图态,不进 view_state、不进 Operation。

## 组合逻辑

「目录 + 正文 + 属性」用 `outline` + `document` + `form`,三者同绑 `block`,经全局 `selection[block]` 联动(三铁律,零直连)。百科页(catalog 节点 + 正文)是其上一层组合:`outline`(节点树)选中一个节点 → `document` 渲该节点所属的 block 流。

## 写入规则

所有写入经 `dispatch(commandId, payload)` 带 `type/id/expected_version`:
- 块文本提交 = `entity.update {patches:{[textField]:draft}}`(失焦 CAS)。
- 新增块 = `onAdd`(框架 `entity.add` + `newEntityDefaults`)。
- 删除块 = `entity.delete`(选中块的删除钮)。
- 重排/缩进(M1)= `lib/treeModel.computeReparent` → `entity.batch_update`(单 Operation 一次 ⌘Z、CAS 一致)。
