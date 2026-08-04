# tooltip

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

tooltip.css — ze-tooltip 组件视觉 (02-atoms) — CSS-only shell
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
反色小气泡(surface-inverse 底 + ink-inverse 字),tooltip 层级。
data-placement 控制箭头朝向(视觉);**显隐时机/定位 = React 交互层(运行时),本批只给静态视觉**。
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-tooltip`, `.ze-tooltip__arrow`
