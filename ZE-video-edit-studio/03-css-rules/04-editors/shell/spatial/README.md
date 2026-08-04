# spatial

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

spatial.css — 空间编辑共享基元(04-editors · 已迁空 stub)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
绝对严格分层(P4 batch11):CanvasViewport(.ze-canvas)/ SpatialCanvas(.ze-spatial)/
TransformGizmo(.ze-gizmo)/ MaskCanvas(.ze-mask)是框架级共享 primitive(canvas2d/asset/
image-cleanup 多编辑器复用),其多态 SVG stroke / 棋盘格 / HUD 的令牌样式无法降为 L4 无令牌
结构,已整组上移 L2 组件原子 → 见 03-css-rules/02-atoms/spatial.css(经 index.css @import)。

## 对外表面

无 `.ze-*` class；该对象提供令牌、生成物或文档事实。
