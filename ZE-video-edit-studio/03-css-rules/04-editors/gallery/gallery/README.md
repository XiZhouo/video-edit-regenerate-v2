# gallery

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

gallery.css — gallery 编辑器自有结构(04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: GalleryEditor —— 网格内容区外壳(.ze-editor__gallery-body)+ 对比模式双视口
(.ze-editor__compare*)。缩略图网格 primitive(.ze-thumb* / .ze-thumb-grid / .ze-marquee)
是框架级共享组件,其令牌样式已上移 L2(02-atoms/thumb.css)。
绝对严格分层(P4 batch8):本层零 var(--ze-*)。内距/间距/边框/倒角/底色/字号由 React 贴 L2

## 对外表面

`.ze-canvas`, `.ze-editor__compare`, `.ze-editor__compare-label`, `.ze-editor__compare-pane`, `.ze-editor__filtergroup`, `.ze-editor__gallery-body`
