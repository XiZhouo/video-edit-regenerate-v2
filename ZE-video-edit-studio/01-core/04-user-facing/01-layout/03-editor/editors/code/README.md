# Code editor

代码入口: `CodeEditor.tsx`。

## 说明

结构化文本编辑器。用于 JSON、SQL、脚本或配置片段，带语言标签和行号。

## Core 负责

- codeField/langField 的读取、draft、blur 提交
- 轻量代码视觉：等宽、行号、语言标签

## Core 不负责

- 真正的 LSP、格式化器、沙箱执行器
- 把代码运行结果写入 store；运行必须由 command/pipeline 做

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
