# editor-shell

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

editor-shell.css — ze-editor 编辑器外壳组件原子 (02-atoms / L2 组件原子)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2)
入口: @ze/css/02-atoms/index.css → @import
为什么在 L2:`.ze-editor` 及其外壳子件(toolbar 槽 / statusbar / 空态 / 加载壳 + 空间编辑器
家族共享的 __canvas-body 脚手架)是**全 15 编辑器共享的外壳 chrome**(EditorFrame 统一组装,
见 01-core/.../EditorFrame.tsx)。它是 ZE 里复用面最广的 shell 组件,视觉走令牌(底色/墨色/字体/

## 对外表面

`.ze-bar`, `.ze-button`, `.ze-button__label`, `.ze-editor`, `.ze-editor__canvas-body`, `.ze-editor__canvas-hint`, `.ze-editor__canvas-overlay`, `.ze-editor__canvas-skeleton`, `.ze-editor__content`, `.ze-editor__count`, `.ze-editor__empty`, `.ze-editor__header`, `.ze-editor__loading`, `.ze-editor__search`, `.ze-editor__statusbar`, `.ze-editor__statusbar-counts`, `.ze-editor__statusbar-op`, `.ze-editor__statusbar-reveal`, `.ze-editor__statusbar-toggle`, `.ze-editor__title`, `.ze-editor__toolbar`, `.ze-editor__toolbar-actions`, `.ze-editor__unit`, `.ze-editor__unit-control`
