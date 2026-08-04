# copilot-shell 规则

- 代码只写在本目录的 `code.css`，不要把对象规则散回层级根目录。
- 状态必须挂在对象自己身上：用 `data-side` / `data-open` / `data-busy` / `data-role` 这类事实属性，不靠额外 class 组合。
- 收 / 展 / busy / 换边不改变挂件在页面里的定位契约（始终 `position: fixed`、`z-index: var(--ze-z-modal)`、浮在最上层、`pointer-events` 只落在挂件本体）。
- 全部颜色 / 字号 / 圆角 / 阴影 / 时长走 L1 token（`var(--ze-*)`），禁字面 hex/rgb/oklch；明暗随 `[data-theme=dark]` 自动跟随。
- 只写结构与状态类；业务语义（谁在说话、说什么）由组件经 `data-role` 与文本内容表达，不进 class 名。
- L2 UI 可以消费 L1 token；不重新定义原子视觉。
