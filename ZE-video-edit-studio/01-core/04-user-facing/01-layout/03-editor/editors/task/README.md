# Task editor

代码入口: `TaskEditor.tsx`。

## 说明

任务运行列表。它把实体的运行状态、阶段、进度和重跑入口组织成可操作队列。

## Core 负责

- 按状态分组/展示任务、选择任务、显示 statusOf 返回的运行相位
- runCommand 入口和 stagesField 阶段列表展示

## Core 不负责

- 任务调度器、后台队列、阶段语义解释
- pipeline 的 notebook 级日志和端口展示

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
