# scene3d

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

scene3d.css — scene3d 编辑器内容视觉 (04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: 01-core 的 Scene3dEditor(.ze-editor--scene3d)。
共享 body 脚手架(.ze-editor__canvas-body)在 editor.css;
外壳工具(复位视角/线框)走 .ze-editor__toolbar-actions 右组容器(editor.css)。
绝对严格分层(P4):本文件 **零令牌** —— 渲染器宿主只定盒子结构,视觉由 mod 渲染器内部画。

## 对外表面

`.ze-scene3d-host`
