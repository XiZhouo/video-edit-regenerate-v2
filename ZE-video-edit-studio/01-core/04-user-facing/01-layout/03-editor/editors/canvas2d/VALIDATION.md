# Canvas2d editor validation rules

## 子模块结构

- 必须存在 `Canvas2dEditor.tsx`，这是该 editor 的代码入口。
- 必须存在 `README.md`、`CONSUMPTION.md`、`VALIDATION.md`。
- 子模块可以放自己的测试，但共享 primitives/zones/lib 仍留在 `03-editor/` 根层。

## 机器校验

- canvas2d 子模块不得把 gallery/form 当内部组件 import
- layerType 场景必须通过 selection[layerType] 联动，不造第二套 activeLayer store
- 像素输出必须是 asset_ref 或 command payload，不能把大图塞进 entity JSON

## 人工审查口径

新增能力先问三件事：它是不是这个 editor 的通用工作面能力；能不能通过 `view_state`/command 接缝表达；如果需要和另一个 editor 同屏，是否应该上移到 L5 LayoutTree，而不是在本 editor 内嵌。
