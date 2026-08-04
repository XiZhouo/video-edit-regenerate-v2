# Document editor

块文档编辑器(`document` class)。把 block 实体(`parent` / `order` 单父树)渲成 Notion/Obsidian/飞书 式文档流。

## 职责

- **块 = L2 atom = 实体**:每种块的视觉只住 L2(`.ze-text[data-prose]` 经后代选择器排版 markdown 注入的标题/列表/引用/码/分隔/链),本编辑器把块实体映射成对应裸标签 HTML(块注册表 `blockRegistry`)。
- **Notion 式逐块内联编辑**:聚焦块 = 源 markdown 文本域、失焦 = 渲染视图(规避 contentEditable 的光标/IME 坑)。
- **选择驱动联动**:与 `outline`(目录)/ `property`(块属性)经 `selection[block]` 联动 —— 选中谁,属性面板/目录就指谁。互不 import(L3 三律)。

## 不是什么

- 不是 `text`:text 编辑单实体的一个长文字段;document 编辑一棵 block 实体流。
- 不是 `html`:html 渲染沙箱 raw HTML(AI 出页);document 渲染 markdown 块文档(结构化、逐块可编)。
- 不是 `outline`:outline 只管层级/选择;document 渲文档内容 + 逐块编辑。二者同绑 block,经 L5 组合成「目录 + 正文」。

## 组合

`header`(HeaderZone:类型名 + 块计数) + `body` 内容区(块流 + 新增条)。相邻 slot 常放 `outline`(同 block 的目录树)与 `form`(选中块属性)。

## 模型

- 文档顺序 / 嵌套走 `lib/treeModel.ts`(`flattenVisible`,复用 outline 同一纯逻辑)。
- markdown 渲染走 `lib/markdown.ts`(零依赖、XSS 安全)。
- 块类型 → 渲染走 `editors/document/blockRegistry`(M0 内置 paragraph/heading/divider;M1 转开放集 + mod 注入)。

## 里程碑

- **M0(本批)**:渲块流 + 选择 + 逐块内联编辑(draft/commit CAS)+ 增(onAdd 接缝)/删(entity.delete)。
- **M1**:拖拽重排/缩进(`computeReparent → entity.batch_update`)+ 4 新块种(callout/code/checklist/figure 各 L2 原子)+ slash 菜单 + 改类型 + toggle 折叠。
- **M2**:行内富标记真 WYSIWYG(`view_state.richEditorModuleId` 懒加载)+ md↔blocks Reader/Writer。
