# table

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

table.css — ze-table 数据表组件原子 (02-atoms / L2 组件原子)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2)
入口: @ze/css/02-atoms/index.css → @import
为什么是 L2 组件原子:表格样式天然靠 th/td/tr + :hover + 后代选择器(每个单元格贴 className 不
现实),无法拆成可贴的小原子。故整张表收成一个 `.ze-table` 组件原子(同 .ze-card/.ze-badge):
容器贴 .ze-table,内部 th/td/tr/选中/run-phase 由本原子经后代选择器统一给样式。令牌消费收敛此层,

## 对外表面

`.ze-table`
