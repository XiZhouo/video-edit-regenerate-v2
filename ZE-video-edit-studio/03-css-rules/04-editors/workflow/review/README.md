# review

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

review.css — review 编辑器内容视觉 (04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: 01-core 的 ReviewEditor(.ze-editor--review)。基座在 editor.css。
绝对严格分层(P4 去令牌):本文件 **零令牌**。竖排/内距=React `.ze-stack[data-gap][data-pad]`;
动作行=`.ze-row`;标签=`.ze-text[data-role=secondary]`;状态徽章=`.ze-badge[data-variant][data-emphasis]`;
文本域=`.ze-textarea`。本层只留无令牌结构(填充/居中/不可缩放)。

## 对外表面

`.ze-review__body`, `.ze-review__compare`, `.ze-review__note`
