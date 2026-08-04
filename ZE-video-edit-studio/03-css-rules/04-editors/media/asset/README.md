# asset

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

asset.css — asset 编辑器内容视觉 (04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: 01-core 的 AssetEditor(.ze-editor--asset)。
asset 的空间变换沿用共享基元(spatial.css 的 .ze-spatial / .ze-gizmo);
变换工具走 .ze-editor__toolbar-actions 右组容器(editor.css)。
绝对严格分层(P4 去令牌):本文件 **零令牌(无 var())**。非图片文件信息块的竖排 + 内距由 React 贴

## 对外表面

无 `.ze-*` class；该对象提供令牌、生成物或文档事实。
