# timeline 规则

- 代码只写在本目录的 `code.css`，不要把对象规则散回层级根目录。
- 状态必须挂在对象自己身上：优先使用 `data-ui-state`、`data-run-phase`、`aria-invalid`、`disabled` 这类事实属性。
- 创建中的区间只用 `.ze-timeline__clip[data-draft="true"]`；它不是耐久实体，不得同时伪装成 selected、running 或 error。调用方只可内联运行时位置/尺寸和局部 `--clip-color`，不得内联透明度、轮廓、颜色或 `pointer-events`。
- 同一对象的 normal / empty / running / error / stale / disabled 不能改变外盒尺寸；只改变颜色、描边、透明度、覆盖层或内容可见性。
- draft 必须保持事件穿透，避免预览片段遮挡轨道自身的 pointer capture / pointerup 提交链；该规则属于 L2 timeline 原子，不由 editor 或 mod 覆盖。
- L2 UI 可以消费 L1 token；L3/L4/L5 只能组合 L2 对象和结构，不重新定义原子视觉。
