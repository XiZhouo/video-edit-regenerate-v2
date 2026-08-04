# icon-button

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

icon-button.css — ze-icon-button 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
视觉 DNA: Header/Footer 的方形图标按钮(唯一图标按钮组件,统一名 .ze-icon-button)。
- 方形,transparent base + overlay-soft hover;data-active 做 toggle 选中态
- 尺寸 token-pure:sm=24/md=28/lg=32(--ze-space-6/7/8),不造 px 字面量

## 对外表面

`.ze-icon-button`, `.ze-icon-button__icon`
