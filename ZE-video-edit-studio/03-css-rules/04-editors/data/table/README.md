# table

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

table.css — table 编辑器内容视觉 (04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: 01-core 的 TableEditor → TableZone(.ze-editor--table)。基座在 editor.css。
绝对严格分层(P4 去令牌):本文件 **零令牌**。表格本体(th/td/tr/hover/选中/run-phase)收成 L2
组件原子 `.ze-table`(02-atoms/table.css);本层只留无令牌结构:滚动归属、行操作列对齐、
排序箭头(走 .ze-text[data-tone=accent])、以及响应式 max-width 覆盖(字面 px,容器查询)。

## 对外表面

`.ze-editor__row-actions`, `.ze-editor__tbl`, `.ze-editor__tbl-scroll`
