# state-frame 规则

- 状态来自数据事实，不来自视觉猜测。后端 entity/run 至少要能表达 `empty`、`running`、`normal`、`error`。
- 状态挂在对象自身；卡片、行、缩略图、字段行、菜单项、表格行都可以直接带 `data-ui-state`。
- 状态变化不能改变外盒尺寸。允许改 `color`、`background`、`border-color`、`border-style`、覆盖层和透明度。
- `running` 不锁死对象交互；是否禁用由业务另行给 `disabled` / `aria-disabled`。
