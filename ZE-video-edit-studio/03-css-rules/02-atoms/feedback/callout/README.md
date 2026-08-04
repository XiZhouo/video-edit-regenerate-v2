# callout

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

callout.css — ze-callout 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
视觉 DNA: 对齐 v2 .ze-callout —— 左 3px 语义条 + 语义 wash 底 + 标题/正文。
语义底/条用 color-mix 就地推导(不加新 令牌):
底 = color-mix(in oklab, semantic 10~12%, surface-sunken)

## 对外表面

`.ze-callout`, `.ze-callout__body`, `.ze-callout__icon`, `.ze-callout__title`
