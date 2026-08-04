# spatial

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

spatial.css — ze-canvas/ze-spatial/ze-gizmo/ze-mask 空间编辑基元组件原子 (02-atoms / L2 组件原子)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2)
入口: @ze/css/02-atoms/index.css → @import
为什么在 L2:CanvasViewport(.ze-canvas)/ SpatialCanvas(.ze-spatial)/ TransformGizmo
(.ze-gizmo)/ MaskCanvas(.ze-mask)是框架级共享 primitive(canvas2d / asset / image-cleanup
多编辑器复用,见 01-core/.../primitives/)。其视觉核心是「多态 SVG stroke」:bbox 选中切

## 对外表面

`.ze-badge`, `.ze-canvas`, `.ze-canvas__empty`, `.ze-canvas__hud`, `.ze-canvas__img`, `.ze-canvas__stage`, `.ze-gizmo`, `.ze-gizmo__arm`, `.ze-gizmo__frame`, `.ze-gizmo__handle`, `.ze-layered-canvas`, `.ze-layered-canvas__bbox`, `.ze-layered-canvas__bg`, `.ze-layered-canvas__crop`, `.ze-layered-canvas__cursor`, `.ze-layered-canvas__hud-brush`, `.ze-layered-canvas__layer`, `.ze-layered-canvas__paint`, `.ze-layered-canvas__selpath`, `.ze-layered-canvas__shape`, `.ze-layered-canvas__stage`, `.ze-layered-canvas__text`, `.ze-layered-canvas__viewport`, `.ze-layered-canvas__workbench`, `.ze-mask`
