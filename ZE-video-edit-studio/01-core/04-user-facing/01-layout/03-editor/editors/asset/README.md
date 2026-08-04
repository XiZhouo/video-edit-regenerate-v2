# Asset editor

代码入口: `AssetEditor.tsx`。

## 说明

单资产查看器。用于看图、PDF、下载、复制，以及触发少量资产变换动作。

## Core 负责

- 从 entity.data 中挑 asset_ref 并解析 URL
- 图片/非图片预览、元信息、复制/下载 UI
- transformCommand 的动作按钮派发

## Core 不负责

- 真正的像素变换、文件存储、资产版本管理
- 多素材列表；多条资产用 gallery/table 选，再由 asset 查看

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
