# tool-themes 规则

- 代码只写在本目录的 `code.css`，不要把对象规则散回层级根目录。
- 状态必须挂在对象自己身上：优先使用 `data-ui-state`、`data-run-phase`、`aria-invalid`、`disabled` 这类事实属性。
- 同一对象的 normal / empty / running / error / stale / disabled 不能改变外盒尺寸；只改变颜色、描边、透明度、覆盖层或内容可见性。
- L2 UI 可以消费 L1 token；L3/L4/L5 只能组合 L2 对象和结构，不重新定义原子视觉。
