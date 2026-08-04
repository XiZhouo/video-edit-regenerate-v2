# divider

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

divider.css — ze-divider 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
视觉 DNA: 对齐 v3 footer 的 segment-divider —— 1px overlay-soft 细线。
支持水平/竖直/虚线/带标签四态;无 data-size。
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-divider`
