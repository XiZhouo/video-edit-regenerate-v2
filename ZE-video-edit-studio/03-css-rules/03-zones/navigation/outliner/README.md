# outliner

层级：L3 功能区。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

outliner.css — outliner 功能区(大纲 / 层级树)结构 (03-zones)
zone: outliner · 默认位置 editor 左侧 · 职责 实体列表 / 层级树 / 大纲
绝对严格分层(DESIGN-SYSTEM §0):本文件 **零 var(--ze-*)** —— 视觉全走 L2 原子:
容器 = .ze-stack[data-surface=raised];节点 = .ze-menu-item(hover + data-active 选中);
文字 = .ze-text[data-role=secondary]。本文件只留"树特有的、无令牌的结构":
滚动归属、caret 折叠转向、窄容器隐藏 label。缩进深度由 React 行内 paddingLeft 给(数据驱动)。

## 对外表面

`.ze-outliner`, `.ze-outliner__caret`, `.ze-outliner__label`, `.ze-outliner__node`
