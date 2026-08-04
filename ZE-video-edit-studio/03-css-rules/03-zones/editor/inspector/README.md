# inspector

层级：L3 功能区。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

inspector.css — inspector 功能区结构:SectionTitle / Collapsible / ParamPanel (03-zones)
inspector 是属性检查面板,作为独立 form 编辑器在 L5 布局组合里与主编辑器并排(选择驱动)。
绝对严格分层(DESIGN-SYSTEM §0):本文件 **零 var(--ze-*)** —— 视觉/排版全走 L2 原子:
段标题 = .ze-bar + .ze-text[data-role=title];折叠头 = .ze-disclosure(▸ caret 随
aria-expanded 旋转;标题 = 子 .ze-text[data-role=title]);段体/参数栈 =
.ze-stack[data-gap][data-pad];滑杆行 = .ze-row(读数 = .ze-text.ze-field-row__value);

## 对外表面

`.ze-param`
