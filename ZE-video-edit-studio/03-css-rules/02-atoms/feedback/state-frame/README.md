# state-frame

层级：L2 UI。代码入口：`code.css`。

## 说明

`state-frame` 定义的是 UI 对象自己的生命周期显示口径，不是一个独立的“空态组件”。后端 entity 或 run 记录给出状态，前端把事实挂到对象自己的 `data-ui-state` / `data-run-phase` 上，对象自己显示。

状态闭集先按这六类落地：`normal`、`empty`、`running`、`error`、`stale`、`disabled`。后面要加新状态，先补 L1 `--ze-state-*`，再加这里的映射。

## 对外表面

`.ze-state-frame`、`.ze-state-frame__content`、`.ze-state-frame__message`、`.ze-state-frame__indicator`，以及已有对象上的 `[data-ui-state]` / `[data-run-phase]`。
