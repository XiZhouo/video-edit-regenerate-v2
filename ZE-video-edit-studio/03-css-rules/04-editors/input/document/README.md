# document

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

document.css — document(块文档)编辑器内容视觉 (04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: 01-core 的 DocumentEditor(.ze-editor--document)。基座在 editor-shell(L2)。
绝对严格分层(L4 零令牌)：本文件 **零 var(--ze-\*)**。块视觉=L2 `.ze-text[data-prose]`（markdown 注入的
h/p/列表/引用/码/hr/链由后代选择器排版）；竖排=React `.ze-stack`；按钮=`.ze-button`；文本域=`.ze-textarea`。
本层只留无令牌结构（填充/滚动/缩进定位锚/绝对定位删除钮）。块缩进 padding 经 JSX 行内 `calc(令牌×depth)`。

## 对外表面

`.ze-document__body`, `.ze-document__block`, `.ze-document__view`, `.ze-document__edit`, `.ze-document__add`, `.ze-document__del`
