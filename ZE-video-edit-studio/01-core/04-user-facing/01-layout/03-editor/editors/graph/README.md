# Graph editor

代码入口: `GraphEditor.tsx`。

## 说明

节点图编辑器。把实体作为节点，字段里的依赖关系作为边，用于流程、关系、状态图。

## Core 负责

- x/y/label/deps 字段映射、节点选择、拖动定位、双击新增、删除
- GraphCanvasZone 的画布交互和 edge 渲染

## Core 不负责

- 自动布局算法、流程执行、复杂图数据库查询
- pipeline notebook 的 step 日志

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
