# guide-shell

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

guide-shell.css — 引导精灵视觉（`.ze-guide*`，02-atoms / L2 组件原子），系统 4『变形体 v2』。
设计真源：《ZE精灵变形体视觉设计-20260718》（用户定稿，参数照搬）。权威契约：`MOTION.md` 精灵章。

精灵 = **一个变形虫式单实体**：白芯蓝边（`--ze-guide-fill` 芯 + `--ze-accent-500` 边），
球 ⇄ 三角光标 ⇄ 环形矩形三形态**严格连续变形**（28 点系逐点插值）；光只有一层 = 移动余晖
（canvas 每帧重建、按时长精确消散）。圈选 = 本体长成实体环带（evenodd 镂空、环外压暗、
环内可读、沿环发光）。飞行 = 贝塞尔弧线 + 惯性转弯 + 果冻拉伸。

几何/位姿/变形全部由渲染引擎 `webapp/src/lib/spriteEngine.ts` 在 rAF 内驱动（path d /
viewBox / left/top / transform）；**本 css 只管静态皮肤**（填色/描边/层叠/涟漪/launcher 小球/
气泡）与 reduced-motion 降级。旧 4 层 Siri 美术（aura/core/spark/ring + 12 keyframes）已于
css 20.0.0 移除。

协议：03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md（§3 L2）
入口：@ze/css/02-atoms/index.css → @import
组件：webapp/src/GuideLayer.tsx（装配）+ webapp/src/lib/spriteEngine.ts（引擎）

为什么在 L2：与 copilot-shell 同类——框架级悬浮 chrome，含状态化皮肤与降级合同，
无法降为 L4 无令牌结构。纯装饰、`aria-hidden`；真执行永远在命令总线，本层不含任何行为。

## 对外表面

`.ze-guide`, `.ze-guide__stage`, `.ze-guide__trail`, `.ze-guide__glow`, `.ze-guide__svg`,
`.ze-guide__shape`, `.ze-guide__veil`, `.ze-guide__wash`, `.ze-guide__rip`, `.ze-guide__mini`,
`.ze-guide__sprite`, `.ze-guide__bubble`, `.ze-guide__say`, `.ze-guide__note`, `.ze-guide__meta`,
`.ze-guide__interactive`, `.ze-guide__launcher`, `.ze-guide__actions`,
`.ze-guide__picker`, `.ze-guide__picker-title`, `.ze-guide__picker-item`,
`.ze-guide__picker-when`, `.ze-guide__picker-empty`

状态属性（挂 `.ze-guide__sprite` 气泡锚点容器，供测试/调试观察；形态几何由引擎驱动）：
- `[data-form=orb|cursor|frame]`（FSM 派生形态）。
- `[data-state=hidden|appearing|idle|moving|pointing|framing|acting|dismissing]`（8 态机）。

令牌：`--ze-z-guide`（层级）· `--ze-spring`（气泡锚点回弹）· `--ze-guide-fill/veil/wash/
trail-hi/shadow/frame-glow`（变形体材质，全部从 accent/主题派生）。
