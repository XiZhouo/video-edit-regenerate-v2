# chart

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

chart.css — chart 编辑器自有结构(04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: ChartZone —— 量化摘要 KPI strip(.ze-chart__kpis,内含 .ze-status-tile)。
SVG 图表可视化 primitive(frame/plot/svg + 轴/柱/线/面积/点/切片 + crosshair/tooltip)
是框架级内容区组件,其多态/多系列令牌样式已上移 L2(02-atoms/chart.css)。
绝对严格分层(P4 batch9):本层零 var(--ze-*)。KPI strip 的间距/内距/分隔线由 React 贴 L2

## 对外表面

`.ze-chart__kpis`
