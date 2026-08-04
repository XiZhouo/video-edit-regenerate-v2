# Review editor

代码入口: `ReviewEditor.tsx`。

## 说明

单实体审阅面板。用于 approve/reject、批注，以及可选的前后对比。

## Core 负责

- statusField/noteField 的读取和提交
- approve/reject UI、批注 textarea、可选 CompareZone

## Core 不负责

- 审核流转、权限、通知、复杂标注系统
- 纯对比 viewer；无决策的对比用 compare editor

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
