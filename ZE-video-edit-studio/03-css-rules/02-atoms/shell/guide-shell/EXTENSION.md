# guide-shell 扩展

本对象是框架级引导层视觉，随 `skill_sync` 下发到全部下游 v3 web skill，工具侧不改本文件。

## 允许的扩展

- 新增精灵形态：在 `spriteEngine.ts` 的 28 点系形状库加新形状（保持同点数可逐点互插），`data-form` 加取值；皮肤侧只补必要的静态样式，不新增并列 class。
- 新增生命周期/过渡态：加 `data-state` 取值；动态行为进引擎，皮肤只管降级与静态观感。
- 主题：换 accent 色只改 L1 token 种子，`--ze-guide-*` 全部从 accent/主题派生自动跟随，不在此写死色值。
- 调定稿参数：唯一入口 = 设计稿调参面板拍新值 → 回填 `SPRITE_PARAMS` + 设计稿 §5 + 相关 filter 令牌（如 `--ze-guide-frame-glow` 的 px 档）。

## 预留挂点（本期未实现，扩展时在此对象内收口）

- AI 改动回执/确认拉起的专属观感微调（沿用三形态语义，不新增第四形态）。
- 语音/朗读指示：气泡 `__bubble` 内补朗读态，走 `data-*` 状态，不新增并列 class。

## 不允许

- 不在组件 `.tsx` 里内联**装饰性**视觉样式（rule 8 LAYER_INLINE_VISUAL）；引擎 rAF 写的 path/位姿/透明度属状态输出，颜色一律经 class/token。
- 不在皮肤外（webapp / 01-core / 02-mod）新建 `.css`（rule 3 CSS_OUTSIDE_SKIN）。
- 不把引导语义写进 class 名（zone-class 门禁），指哪/说什么只经 `data-*` 与文本表达。
- **不引入任何行为**：不合成 DOM 事件、不派发命令、不持有 dispatch 引用；act 仅涟漪视觉，真执行永远在命令总线。
- 不复活旧 4 层 Siri 美术（`__aura`/`__core`/`__spark`/`__ring`，css 20.0.0 已移除）；旧 `--ze-orb-a/b/c` 等令牌仅为下游过渡保留，禁新增消费。
- 不重定义 `--ze-spring`；不绕开 `SPRITE_PARAMS` 在别处写第二份参数。
