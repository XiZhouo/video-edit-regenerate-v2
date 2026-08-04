# Canvas2d editor consumption rules

## Layout 声明

```ts
slot('canvas', 'canvas2d', ['layer'], { preset: 'ps-lite', layerType: 'layer', bgField: 'sourceRef', paintCommand: 'artwork.paint_layer', exportCommand: 'artwork.export_png' })
```

## `view_state` 消费规则

- preset/features/tools: 收窄工作面能力，不决定业务算法
- bgField: 底图 asset_ref 字段
- geometryField 或 xField/yField/wField/hField/rotationField: 开启 spatial 几何编辑
- layerType: 使用某个实体桶作为图层源；active layer 来自全局 selection[layerType]。layer 可带 `parentId/order` 形成树,canvas2d 会按树状顺序拍平成画布栈,并让子层继承父层 hidden/locked。
- importCommand/layerAddCommand/paintCommand/exportCommand/actionCommand: 所有写入和业务动作入口
- pixelLayerField/pixelLayerCommand: 正式像素层提交接缝；画笔/橡皮提交整张 PNG，mod 决定这张图后续怎么消费
- selectionLayerField: 正式选择像素层字段；用于粗选、范围层、alpha 输入等“用户画出一张范围图”的场景
- maskField/rangeMaskField/maskCommand: legacy alias；仍可读写同一张像素层，但新 mod 不再用这些名字表达 core 逻辑
- `role` / `meta.role`: 可标记 annotation/reference/candidate/output 等业务角色；core 只显示和编辑 layer,不解释这些角色怎么进入 AI prompt。
- `filters[]`: 多字段滤镜栈；`filter` 是旧单滤镜兼容字段。
- `meta.style`: 通用绘制样式,支持 `stroke/fill/color/fontSize/strokeWidth/cornerRadius`。
- `exportCommand`: 导出可见合成图时 dispatch `{type,id,image,scope,width,height,layerId?,expected_version}`；skill 决定该 asset_ref 后续是生成输入、预览还是导出件。

## 两个功能区

- 画布工具功能区：放 brush/eraser/brush size/opacity/undo/redo/clear/run 等工具状态和命令入口。它只改变当前编辑动作，不改变业务语义。
- 2D 画布功能区：放视口、底图、图层投影、移动/缩放/旋转、像素采样、笔画合成和提交。它只产出字段或图层里的 asset_ref。

这两个功能区都在 canvas2d 内部完成。gallery/form/review 等外部 editor 只通过全局 selection 和 command 与它联动，不能把自己的私有 toolbar 塞进 canvas2d。

## 组合逻辑

完整 2D 工作台 = gallery(flat 图层/候选列表) 或 outline(tree/group 图层) + canvas2d(画布) + form(属性)。如果只做 bbox 标注，则 canvas2d 绑定 box 实体并声明几何字段。标注本身也可以是 `layer` 实体(`role:'annotation'` 或 `meta.role:'annotation'`),发送给 AI 时怎么过滤、合成、拼 prompt,都属于 mod command/pipeline。

## 写入规则

所有写入都必须经过 `dispatch(commandId, payload)`。如果某个字段能回退到内置 `entity.update/entity.delete`，也仍然要带 `type/id/expected_version`，让 undo、CAS、运行态和审计链路保持一致。
