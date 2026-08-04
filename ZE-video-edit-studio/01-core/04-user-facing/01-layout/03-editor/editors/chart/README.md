# Chart editor

代码入口: `ChartEditor.tsx`。

## 说明

轻量数据图表。用于把同类实体的数值字段快速汇总成 bar/line/scatter/pie 等视图。

## Core 负责

- 从实体数组里解析 x/y/label/metric 字段
- KPI 摘要、图表空态、点击选中实体

## Core 不负责

- 重型图表库、交互缩放、业务指标口径
- 数据查询；chart 只展示已经进入 document store 的实体

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
