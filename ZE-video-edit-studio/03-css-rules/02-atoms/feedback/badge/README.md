# badge

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

badge.css — ze-badge 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
视觉 DNA: pill 小标签。对齐 v3 footer 的 count badge(md 16px / sm 14px / 9px 字)。
语义变体用 color-mix wash 底;data-emphasis="solid" 给高强调实底(如错误计数)。
16/14/8px 几何字面量对齐 footer count badge 先例(令牌层无此尺寸 token)。

## 对外表面

`.ze-badge`
