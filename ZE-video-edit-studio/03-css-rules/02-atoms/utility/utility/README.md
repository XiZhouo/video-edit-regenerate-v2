# utility

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

utility.css — ze-* 视觉实用原子族 (02-atoms / L2 实用原子)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2)
入口: @ze/css/02-atoms/index.css → @import
为什么有它:绝对严格分层(除 L2 外不碰令牌)落到 L4 编辑器 / L5 chrome 后,这些层的 .css
不准再直接写 var(--ze-overlay-soft) 画边框、var(--ze-radius-*) 倒角、var(--ze-accent-*) 上色、
var(--ze-dur-*) 做过渡。本族把"边框 / 倒角 / 底色 / 内距 / 色调 / 过渡 / 阴影"这些**可贴任意

## 对外表面

`.ze-dotgrid`, `.ze-edge`, `.ze-elevate`, `.ze-gap`, `.ze-inset`, `.ze-pad`, `.ze-round`, `.ze-row-card`, `.ze-runstate`, `.ze-scrollbar`, `.ze-scroll-fade-y`, `.ze-scroll-fade-x`, `.ze-scroll-fade-both`, `.ze-selectable`, `.ze-size`, `.ze-surface`, `.ze-viewport`
