# guide-shell 验收

- [ ] 待机态在角落显示「教我用」`.ze-guide__launcher` 角标（`.ze-guide__mini` 白芯蓝边小球呼吸）；点开出 `.ze-guide__picker` tour 列表（空则 `.ze-guide__picker-empty`）。
- [ ] 收到引导帧后精灵出现：单实体白芯蓝边球（spring 放大入场），待机呼吸 2.4s + 柔光。
- [ ] 换目标：球沿**弧线**飞行（非直线）、沿速度方向果冻拉伸、身后留雾状余晖并按时长精确消散零残留；中途改道走惯性转弯（速度连续不折线）。
- [ ] `point` 步 + 有锚点 → 飞到目标上方**变三角光标**指着它；`act` 步 → 飞到目标中心落地压扁回弹 + `.ze-guide__rip` 涟漪 + tick 一声，停 1.25s 后收到目标上方悬停。
- [ ] `highlight` 步 + 有锚点 → 本体连续**长成环形实体矩形**（白环带 + 内外蓝边 + 沿环发光 `--ze-guide-frame-glow`）；环外压暗（`--ze-guide-veil`）、环内可读并轻洗色（`--ze-guide-wash`）；目标移动/滚动时环平滑跟随。
- [ ] `.ze-guide__bubble` 气泡显示 `__say` 文案 + `__meta` 步计数（+ 可选 `__note`），锚点随精灵停泊点滑动。
- [ ] 退场：球缩小淡出 380ms；缺锚点回落球「只说不指」。
- [ ] 全部计算样式为 `var(--ze-*)` token 解析值（无字面 hex/rgb）；暗/浅双主题 + 36 档 accent 跟随（白芯不变、边/光随 accent）。
- [ ] `prefers-reduced-motion: reduce` 或 `html[data-motion=off]`：瞬移、关余晖/拉伸/变形动画/涟漪/音效；位置、圈选压暗、气泡、信息仍在。
- [ ] `z-index: var(--ze-z-guide)`；除 launcher/picker/气泡外 `pointer-events` 不拦截页面。
- [ ] 机械门禁：css-contract / css-layer-guard / css-module-structure / zone-class 全绿；CSS_OUTSIDE_SKIN 无本对象命中。
