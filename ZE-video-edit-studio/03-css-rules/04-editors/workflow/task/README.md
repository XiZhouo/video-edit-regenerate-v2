# task

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

task.css — task 编辑器看板行视觉:TaskRow (04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: 01-core 的 TaskEditor → ListZone(.ze-editor--task)。进度环/徽章/错误占位用 02-atoms。
绝对严格分层(P4 去令牌):本文件 **零令牌**。行卡边框/底色/倒角/内距/间距/最小高/选中态 =
`.ze-row-card`+`.ze-surface`+`.ze-round`+`.ze-pad`+`.ze-gap`+`.ze-size`;字号/字重/色 = `.ze-text`;
阶段 pill = `.ze-round[pill]`+`.ze-surface[sunken]`+`.ze-text[data-tone]`。本层只留无令牌结构

## 对外表面

`.ze-editor__task-body`, `.ze-editor__taskfilter`, `.ze-task-row`, `.ze-task-row__name`, `.ze-task-row__stage-dot`, `.ze-task-row__stage-label`, `.ze-task-row__stage-pill`, `.ze-task-row__stages`
