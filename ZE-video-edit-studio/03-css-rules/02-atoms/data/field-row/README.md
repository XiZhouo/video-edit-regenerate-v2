# field-row

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

field-row.css — ze-field-row 表单字段行组件原子 (02-atoms / L2 组件原子)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2)
入口: @ze/css/02-atoms/index.css → @import
为什么在 L2:「label + 控件」的字段行是框架级共享 primitive(FormZone schema 驱动表单 +
ParamPanel 参数面板复用)。它承载行高系统(SPEC §4):单行字段统一 min-height=row-h、控件
垂直居中不撑高行;唯一例外多行控件(textarea/JSON)挂 data-multiline → 高度 = n×row-h、顶

## 对外表面

`.ze-checkbox`, `.ze-field-row`, `.ze-field-row__color`, `.ze-field-row__label`, `.ze-field-row__value`, `.ze-param`, `.ze-row`, `.ze-skeleton`, `.ze-textarea`

密度：`data-density="compact"` 用于属性面板/参数面板，把 label 列和字段间距收紧；默认字段行保持标准表单节奏。
