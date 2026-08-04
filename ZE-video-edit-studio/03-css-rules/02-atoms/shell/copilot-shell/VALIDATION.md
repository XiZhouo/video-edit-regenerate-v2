# copilot-shell 验收

- [ ] 折叠态在页面角落显示精灵停靠态小球（`.ze-copilot__orb-core` 白芯蓝边 `--ze-guide-fill`+accent 描边+柔光,与 guide-shell 变形体同体材质,呼吸动效;busy=呼吸加速 `--ze-dur-breathe-fast`）。
- [ ] 双击小球 → `[data-open=true]`，面板展开、小球隐藏；收起 → 回到小球。
- [ ] `[data-side=left]` / `[data-side=right]` 分别贴左下 / 右下角，换边即时生效。
- [ ] 对话流 `.ze-copilot__transcript` 溢出时出现真实滚动条，可上翻历史。
- [ ] 气泡按 `[data-role]` 区分：user 靠右 accent 底 + inverse 字；assistant 靠左 sunken 底；system 用 danger-wash。
- [ ] `[data-busy=true]` 时小球 / pulse 加速脉冲，思考气泡显示三点动画 + 文案。
- [ ] 全部计算样式为 `var(--ze-*)` token 解析值（无字面 hex/rgb）；暗色主题下自动跟随。
- [ ] `prefers-reduced-motion: reduce` 下关闭呼吸 / 点 / 进场动画。
- [ ] 机械门禁：css-contract / css-layer-guard / css-module-structure / zone-class 全绿。
