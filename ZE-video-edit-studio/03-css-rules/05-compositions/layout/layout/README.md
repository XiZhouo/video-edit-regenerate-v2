# layout

层级：L5 编辑器组合。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

layout.css — LayoutTree 中区结构(04-editors · 已迁空 stub)
协议: 01-core/04-user-facing/01-layout/SPEC.md §2.2
绝对严格分层(L5 chrome 轮 batch1):LayoutTree 中区根 / 二叉分屏(Split + 可拖分割条)/
EditorSlot 容器是框架级布局 primitive(LayoutRenderer 渲染),分割条状态化(hover/dragging→accent)
+ slot chrome 悬浮显隐,令牌样式无法降为 L4 无令牌结构,已整组上移 L2 组件原子 →
见 03-css-rules/02-atoms/layout-tree.css(经 index.css @import)。

## 对外表面

无 `.ze-*` class；该对象提供令牌、生成物或文档事实。
