# input

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

input.css — ze-input 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
视觉 DNA: 对齐 v2 input + v3 editor__input —— sunken 底 + 1px overlay-soft 边 + accent focus。
约定:表单类 focus 用"内嵌 box-shadow ring"贴圆角(动作类才用 outline)。
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-input`, `.ze-user-picker`, `.ze-user-picker__avatar`
