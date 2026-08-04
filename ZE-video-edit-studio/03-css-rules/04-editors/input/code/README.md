# code

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

code.css — code 编辑器内容视觉 (04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: 01-core 的 CodeEditor → TextZone(variant=code;.ze-editor--code)。基座在 editor.css。
不打包语法高亮(重库),只提供可编辑等宽代码基座。
绝对严格分层(P4 去令牌):本文件 **零令牌**。等宽/字号/行高/底色/边框/内距 全部由 React 贴
L2 原子(.ze-text[data-mono][data-size=xs][data-leading=normal] / .ze-surface / .ze-edge / .ze-pad);

## 对外表面

`.ze-code__body`, `.ze-code__edit`, `.ze-code__gutter`, `.ze-code__textarea`
