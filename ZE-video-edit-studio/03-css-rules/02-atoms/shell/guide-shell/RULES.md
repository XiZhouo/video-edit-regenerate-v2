# guide-shell 规则

- 代码只写在本目录的 `code.css`，不要把对象规则散回层级根目录、更不在皮肤外（webapp）建 `.css`（rule 3 CSS_OUTSIDE_SKIN）。
- **几何归引擎、皮肤归 CSS**：path/viewBox/位姿/旋转/拉伸由 `spriteEngine.ts` rAF 驱动（这是引擎的定位/状态输出，非内联视觉）；颜色/描边/投影/发光/涟漪/降级只住本文件，引擎经 class 与 `var(--ze-*)` 消费，不写死色值。
- 形态变形不改引导层定位契约（始终 `position: fixed`、`z-index: var(--ze-z-guide)`、`pointer-events` 只落在可交互子件如 launcher/picker/气泡，其余 `aria-hidden` 纯装饰不拦截页面）。
- 全部颜色 / 字号 / 圆角 / 阴影 / 时长 / 缓动走 L1 token（`var(--ze-*)`），禁字面 hex/rgb/oklch；明暗随 `[data-theme=dark]` 自动跟随；canvas 余晖需要具体色时经探针解析 token 计算值，仍以 token 为唯一来源。
- 定稿参数（28 点系/飞行/余晖/节拍）唯一真源 = `spriteEngine.ts` 的 `SPRITE_PARAMS`（照搬设计稿 §5，不原创）；改参数 = 改设计，先回设计稿拍板再动代码。
- **纯表现层红线**：本对象不含任何行为——act 步只画 `.ze-guide__rip` 涟漪视觉，绝不合成 DOM 事件、绝不派发命令；真执行永远在命令总线（`source=ai` 经确认闸门）。
- 不重定义 `--ze-spring`（复用上游 motion token）；变形/飞行缓动在引擎内为定稿曲线（easeInOutCubic / easeOutBack(springS)），不落 CSS。
