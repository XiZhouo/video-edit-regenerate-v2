# Pipeline editor

代码入口: `PipelineEditor.tsx`。

## 说明

管线 notebook。它把 command spec、pipeline spec、run、step state、端口和日志组成可运行、可复盘、可导出的工作台。

## Core 负责

- 固定管线的 start cell 参数表单、运行态、step cells、端口输入输出、日志/错误/导出
- 根据 selected entity/runIdField/pipelineId/startCommandId 选择当前 run
- 中断继续、失败重试、单步重跑等 command 入口的 UI

## Core 不负责

- 管线执行器、模型调用、artifact 生成逻辑
- 业务自由选择任意 run 的默认行为；生产界面默认固定管线和当前 entity

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
