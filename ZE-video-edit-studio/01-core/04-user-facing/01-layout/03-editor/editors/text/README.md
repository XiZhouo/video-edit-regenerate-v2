# Text editor

代码入口: `TextEditor.tsx`。

## 说明

单实体长文本编辑器。用于正文、说明、提示词、报告片段等纯文本字段。

## Core 负责

- 本地 draft、预览切换、blur 提交
- 把某个字段的字符串显示为可编辑文本

## Core 不负责

- 富文本 schema、协同光标、文档排版引擎
- 代码语言语义；代码用 code editor

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
