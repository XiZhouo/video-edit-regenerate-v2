# asset-input

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

asset-input.css — ze-asset-input 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
资产挂载控件外壳：dropzone 空态 + 预览框 + 操作区；预览内容(按 mime 分流的 img/video/audio/文件卡)
由 L2 primitive AssetView 渲染进 `__preview` / `__file`。本原子只画「框 + 状态」。
状态走 data-* 闭集(empty/filled/dragover/busy/disabled)，只改颜色/描边/光标，不改外盒尺寸。
全部走令牌(var(--ze-*))；禁止字面量 hex；主题切换仅靠 token。

## 对外表面

`.ze-asset-input`, `.ze-asset-input__frame`, `.ze-asset-input__hint`, `.ze-asset-input__preview`, `.ze-asset-input__file`, `.ze-asset-input__file-name`, `.ze-asset-input__actions`, `.ze-asset-input__file-field`
