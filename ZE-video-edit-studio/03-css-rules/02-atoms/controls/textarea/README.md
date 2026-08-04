# textarea

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

textarea.css — ze-textarea 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
视觉 DNA: 复用 ze-input 的视觉(sunken 底 + accent focus ring),多行差异:
min-height 双行起、resize: vertical、padding 大一档。
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-textarea`
