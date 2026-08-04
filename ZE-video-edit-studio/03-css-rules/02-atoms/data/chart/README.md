# chart

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

chart.css — ze-chart 数据图表可视化组件原子 (02-atoms / L2 组件原子)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2)
入口: @ze/css/02-atoms/index.css → @import
为什么在 L2:ChartZone(框架级内容区 primitive)的 SVG 绘制图元 —— 轴 / 柱 / 线 / 面积 /
散点 / 饼切片 —— 视觉是「多态 + 多系列的 fill/stroke」:hover/selected 切 accent-700、
饼切片按 data-i 循环 6 档语义色。SVG 的 fill/stroke 不是通用 utility 原子(.ze-surface/

## 对外表面

`.ze-chart__area`, `.ze-chart__axis`, `.ze-chart__bar`, `.ze-chart__crosshair`, `.ze-chart__dot`, `.ze-chart__frame`, `.ze-chart__line`, `.ze-chart__plot`, `.ze-chart__slice`, `.ze-chart__svg`, `.ze-chart__tooltip`, `.ze-chart__tooltip-label`, `.ze-chart__tooltip-value`
