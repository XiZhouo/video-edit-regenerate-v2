# Document editor validation

## 纯逻辑(vitest)

- `lib/markdown.test.ts`:零依赖 markdown 渲染(行内粗斜码链 + 块级标题/列表/任务清单/引用/围栏码/分隔线)+ XSS 安全(转义、href 过滤、占位越界保留)+ 围栏语言放宽 + 引用递归深度上限。
- 块顺序/嵌套复用 `lib/treeModel.test.ts`(`flattenVisible`)。

## 真浏览器(gallery showcase)

- block 实体按 `order` 渲成文档流,嵌套 depth 缩进(行内 `paddingInlineStart` calc 令牌×深度)。
- 每块经 prose 容器渲染:heading 大字、paragraph 正文、divider 分隔线;行内 `**粗**` / `*斜*` / `` `码` `` / `[链](url)` 失焦后成样式。
- 点块选中(`data-active` → accent);文本块点进编辑显源 markdown 文本域,失焦提交(版本 +1)。
- 新增条 `+ 添加块` 追加块;选中块右上角删除钮删块。
- 0 console error。

## 门禁

`editor_manifest_check`:document 条目齐(id/class/status/rootSelector/needs + functionalZones selector 在 CSS + fixture 存在 + builtinInteractions 非空 + 子模块三文档)。`editor_frame_check`:走 EditorFrame、无裸根壳、无 import 别的 `*Editor`。`css_layer_guard`:L4 `04-editors/input/document` 零令牌(块视觉走 L2 `.ze-text[data-prose]`,本层只留无令牌结构)。`css_container_contract_check`:`.ze-document__body` `flex:1`+`overflow-y:auto` 带 `min-height:0`。
