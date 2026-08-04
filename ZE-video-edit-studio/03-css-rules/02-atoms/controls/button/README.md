# button

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

button.css — ze-button 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 命名规范 + §2.4 状态矩阵
入口: @ze/css/02-atoms/index.css → @import
视觉 DNA: transparent base + overlay-soft hover + accent press,跟 Header/Footer 一致。
- 变体走 data-variant(primary/secondary/ghost/danger);尺寸走 data-size(sm/md/lg)
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-button`, `.ze-button__icon`, `.ze-button__label`
