# document 规则

- 代码只写在本目录的 `code.css`，不要把对象规则散回层级根目录。
- 块视觉一律走 L2 `.ze-text[data-prose]`（markdown 渲染器注入的语义裸标签由后代选择器排版）；本层不写任何字号/色/边框/背景。
- 状态必须挂在对象自己身上：优先使用 `data-active`、`data-run-phase`、`aria-selected`、`disabled` 这类事实属性。
- 同一对象的 normal / selected / running / error / disabled 不能改变外盒尺寸；只改变颜色、描边、透明度或覆盖层（视觉来源在 L2）。
- L2 UI 可以消费 L1 token；L3/L4/L5 只能组合 L2 对象和结构，不重新定义原子视觉。
- 块缩进用 JSX 行内 `paddingInlineStart: calc(depth × 令牌)`（运行期几何，layer-guard 放行），不在本层写死缩进档。
