# editor

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

editor.css — 03-editor 中区渲染单元【基座】(04-editors · 已迁空 stub)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
绝对严格分层(P4 batch13):编辑器共享外壳 chrome(.ze-editor 根 + toolbar/title/count/
toolbar-actions/search/empty/loading/statusbar/canvas-body 等子件脚手架)是全 15 编辑器共享
的 shell 组件(EditorFrame 统一组装),令牌视觉(底色/墨色/字体/内距/分隔线/状态条)+ 后代/状态
选择器无法降为 L4 无令牌结构,已整组上移 L2 组件原子 → 见 03-css-rules/02-atoms/editor-shell.css

## 对外表面

无 `.ze-*` class；该对象提供令牌、生成物或文档事实。
