# text

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

text.css — text 编辑器内容视觉 (04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: 01-core 的 TextEditor → TextZone(variant=text;.ze-editor--text)。基座在 editor.css。
绝对严格分层(P4 去令牌):本文件 **零令牌**。竖排+内距=React `.ze-stack`+`.ze-pad`;正文/预览
字号色行高 + 行内代码 = `.ze-text`(base + `data-prose` 接管动态注入的 p/code)。控件走 `.ze-textarea`。
本层只留无令牌结构(填充/滚动/不可缩放)。

## 对外表面

`.ze-text__body`, `.ze-text__preview`, `.ze-text__textarea`
