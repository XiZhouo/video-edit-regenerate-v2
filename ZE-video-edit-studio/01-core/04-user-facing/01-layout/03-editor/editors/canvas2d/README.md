# Canvas2d editor

代码入口: `Canvas2dEditor.tsx`。

## 说明

通用 2D 工作面。它能作为纯图像视口、几何框编辑器、图层合成工作区或可绘制像素层容器。

## Core 负责

- 视口缩放/平移、底图 asset_ref 解析、工具栏、几何拖拽和图层舞台
- 把图层实体渲染到画布，把 activeLayerId 读自 selection[layerType]
- 把 brush/eraser 得到的像素结果提交给 command 或 asset upload 接缝
- 维护两类内部功能区：画布工具功能区和 2D 画布功能区

## 两个功能区

- 画布工具功能区：通用工具状态和命令入口，例如 brush、eraser、笔刷参数、撤销、重做、清除、运行。它只表达编辑动作。
- 2D 画布功能区：底图、图层、视口、移动、缩放、旋转、像素采样、笔画合成和提交。它只负责把层准确写回字段或实体。

`pixelLayerField`/`selectionLayerField`/`pixelLayerCommand` 是正式消费接口。`maskField`/`rangeMaskField`/`maskCommand`/`maskSelection` 是历史 alias，只为旧 mod 兼容存在。

## Core 不负责

- 没有一个独立叫 mask 的编辑状态。所谓 mask 只是某个图层或字段里的像素资产，mod 后续拿这些像素做遮罩计算是业务逻辑
- 图层列表和属性面板；它们分别是 cardlist/outline 和 property
- AI 抠图、扩图、修图算法；这里只收集输入并 dispatch command

## 消费入口

mod 不直接把这个组件塞进业务页面；mod 在 LayoutTree 里声明 slot，`slotAdapters.tsx` 把 `view_state` 翻译成 props。稳定 import 仍然是 `@ze/core/04-user-facing/01-layout/03-editor`。
