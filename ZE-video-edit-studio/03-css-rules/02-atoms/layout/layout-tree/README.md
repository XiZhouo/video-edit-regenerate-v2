# layout-tree

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

layout-tree.css — ze-layout-root/ze-split/ze-slot LayoutTree 中区布局组件原子 (02-atoms / L2 组件原子)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2)
入口: @ze/css/02-atoms/index.css → @import
为什么在 L2:LayoutRenderer 渲染的 中区根 / 二叉分屏(Split + 可拖分割条)/ EditorSlot 容器
是框架级布局 primitive(任何用本框架 LayoutTree 的应用都复用,见 01-core/.../LayoutRenderer.tsx)。
其中分割条 `.ze-split__divider` 是状态化的(常态 overlay-soft 底,:hover / [data-dragging]

## 对外表面

`.ze-layout-root`, `.ze-slot`, `.ze-slot__empty`, `.ze-split`, `.ze-split__divider`, `.ze-split__pane`
