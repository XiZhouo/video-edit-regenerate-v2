# progress

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

progress.css — ze-progress 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
进度条:轨道 + 填充。对齐 footer 的进度条视觉(内嵌阴影 + accent 填充)。
data-indeterminate 走平移动画;语义变体改填充色。fill 宽度由 inline width 给(数据)。
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-progress`, `.ze-progress__fill`
