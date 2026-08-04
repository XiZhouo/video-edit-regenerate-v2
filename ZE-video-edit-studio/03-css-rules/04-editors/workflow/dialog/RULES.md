# dialog — 规则

- transcript = message 实体气泡流(Domain Data),按 order 升序;role 驱动 `[data-role]`,发送方 `[data-self=true]` 靠右 accent wash。
- 本文件零字面色:气泡底/边/字一律 var(--ze-*);几何(flex/gap/max-width/px)放行。
- composer 输入用 `.ze-textarea` 原子 + `.ze-dialog__input`(只加 flex/resize 几何),按钮用 `.ze-button`。
- 滚动归 `.ze-dialog__transcript`;气泡不撑破 80% 宽。
