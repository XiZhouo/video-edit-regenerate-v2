# asset-input 规则

- 代码只写在本目录的 `code.css`，不要把对象规则散回层级根目录。
- 状态挂在对象自己身上：`data-state`(empty/filled)、`data-dragover`、`data-busy`、`data-disabled` 这类事实属性。
- 同一对象的 empty / filled / dragover / busy / disabled 不改变外盒尺寸；只改变颜色、描边、光标、覆盖层或内容可见性。
- 预览内容(图/视频/音频/文件卡)由 L2 primitive AssetView 渲染进 `__preview` / `__file`；本原子只画框与状态，不按 mime 分流(分流是 primitive 的职责)。
- L2 UI 可以消费 L1 token；L3/L4/L5 只能组合 L2 对象和结构，不重新定义原子视觉。
