# Compare editor

代码入口: `CompareEditor.tsx`。

## 说明

纯前后对比 viewer。它只展示 before/after 两个 asset，不做审核、不写数据。

## Core 负责

- beforeField/afterField 解析后的 URL 展示
- side-by-side/slider/onion 等 CompareFrame 模式

## Core 不负责

- approve/reject、批注、状态字段
- 图像生成或像素计算

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
