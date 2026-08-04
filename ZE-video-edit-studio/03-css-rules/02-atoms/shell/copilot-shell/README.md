# copilot-shell

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

copilot-shell.css — 常驻悬浮 AI 协作挂件外壳（`.ze-copilot*`，02-atoms / L2 组件原子）。
协议：03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md（§3 L2）
入口：@ze/css/02-atoms/index.css → @import
组件：01-core/04-user-facing/01-layout/copilot/CopilotDock.tsx

为什么在 L2：与 auth-shell / header-shell 同类——框架级 shell chrome，含状态化样式
（精灵停靠态呼吸动效(变形体同体材质,css 20.1 起) / busy 呼吸加速 / hover accent-wash / 浮层阴影 / data-side 换边），
无法降为 L4 无令牌结构。视觉 DNA 复用 `.ze-card`（raised 底 + overlay-soft 边 + 浮层阴影）。

## 对外表面

`.ze-copilot`, `.ze-copilot__orb`, `.ze-copilot__orb-core`, `.ze-copilot__pulse`,
`.ze-copilot__panel`, `.ze-copilot__bar`, `.ze-copilot__title`, `.ze-copilot__bar-actions`,
`.ze-copilot__icon-btn`, `.ze-copilot__transcript`, `.ze-copilot__empty`, `.ze-copilot__msg`,
`.ze-copilot__bubble`, `.ze-copilot__bubble--thinking`, `.ze-copilot__thinking`,
`.ze-copilot__dots`, `.ze-copilot__composer`, `.ze-copilot__context`,
`.ze-copilot__input-row`, `.ze-copilot__input`, `.ze-copilot__send`

状态属性：`[data-side=left|right]`（换边）、`[data-open=true|false]`（收/展）、
`[data-busy=true|false]`（AI 处理中，加速脉冲）、`[data-role=user|assistant|system]`（气泡角色）。
