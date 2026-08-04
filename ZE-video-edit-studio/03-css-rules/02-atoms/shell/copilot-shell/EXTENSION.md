# copilot-shell 扩展

本对象是框架级挂件外壳，随 `skill_sync` 下发到全部下游 v3 web skill，工具侧不改本文件。

## 允许的扩展

- 新增状态：优先加 `data-*` 属性（如未来 `data-voice=recording`），在本 `code.css` 补状态样式；不新增并列 class。
- 主题：换 accent 色只改 L1 token 种子（`--ze-accent-h/-c`），本对象自动跟随，不在此写死色值。

## 预留挂点（本期未实现，扩展时在此对象内收口）

- 语音输入指示：`.ze-copilot__composer` 内补录音态，走 `data-*` 状态。
- 图片附件缩略图行：`.ze-copilot__composer` 上方补 `.ze-copilot__attachments`（结构 + token）。
- 自由拖拽定位：仍用 `position: fixed`，落点吸附到 `data-side`，不引入内联视觉样式。

## 不允许

- 不在组件 `.tsx` 里内联视觉样式（rule 8 LAYER_INLINE_VISUAL）。
- 不把业务语义写进 class 名（zone-class 门禁），角色只经 `data-role` 表达。
- 不在皮肤外（webapp / 01-core / 02-mod）新建 `.css`（rule 3 CSS_OUTSIDE_SKIN）。
