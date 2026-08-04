# layout

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

layout.css — ze-* 布局原子 (02-atoms / L2 布局原子族)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2 布局原子)
入口: @ze/css/02-atoms/index.css → @import
为什么有这一族:绝对严格分层后,L3 功能区 / L4 编辑器的 CSS 不准再直接用 var(--ze-space-*)
排版(那是 L1 令牌)。布局原子把"间距 / 方向 / 对齐 / 网格 / 条 / 撑满"这些排版动作
封成可贴 class 的 L2 积木 —— 上层只组合 .ze-stack/.ze-row/...,把令牌消费收敛在本层。

## 对外表面

`.ze-bar`, `.ze-cluster`, `.ze-fill`, `.ze-grid`, `.ze-row`, `.ze-spacer`, `.ze-stack`
