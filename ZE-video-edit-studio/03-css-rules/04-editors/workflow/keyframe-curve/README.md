# keyframe-curve

层级：L4 编辑器。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

keyframe-curve.css — keyframe-curve 编辑器内容视觉 (04-editors)
SPEC: 01-core/04-user-facing/01-layout/03-editor/SPEC.md
消费: 01-core 的 CurveEditor(.ze-editor--keyframe-curve)。基座在 editor.css。
绝对严格分层：本文件 **零令牌**。点阵底=`.ze-dotgrid`；关键帧点边框/倒角/底色=
`.ze-edge[data-tone=strong]`+`.ze-round`+`.ze-surface`；选中描边=`.ze-selectable`；运行态=`.ze-runstate`；
折线/播放头走 currentColor(色由 svg 贴 `.ze-text[data-role=tertiary]` 给)。点尺寸由组件内联。

## 对外表面

`.ze-curve__scroll`, `.ze-curve__plane`, `.ze-curve__edges`, `.ze-curve__line`, `.ze-curve__playhead`, `.ze-curve__key`
