# app-shell 规则

- 代码只写在本目录的 `code.css`，不要把对象规则散回层级根目录。
- 状态必须挂在对象自己身上：优先使用 `data-ui-state`、`data-run-phase`、`aria-invalid`、`disabled` 这类事实属性。
- 同一对象的 normal / empty / running / error / stale / disabled 不能改变外盒尺寸；只改变颜色、描边、透明度、覆盖层或内容可见性。
- L2 UI 可以消费 L1 token；L3/L4/L5 只能组合 L2 对象和结构，不重新定义原子视觉。
- `.ze-ambient-field`（可选背景画布）是 `.ze-app` 的纯结构子节点：只做定位/层叠（`absolute inset:0`、`z-index:0`，在 Header/Footer/中区之下），零颜色 token——色与动全在 webapp 的 WebGL 组件 `AmbientField.tsx`，不属"原子视觉重定义"。由 App.tsx 依 `.ze-app[data-ze-ambient="field"]` 挂载（opt-in）。
