# chip

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

chip.css — ze-chip 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
chip = 可交互/可删的标签(比 badge 大一档,可点、可选中、可带删除 ×)。
data-selected 选中态走 accent-wash;__remove 是删除按钮槽。
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-chip`, `.ze-chip__icon`, `.ze-chip__remove`
