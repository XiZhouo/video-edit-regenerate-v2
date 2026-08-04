# text

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

text.css — ze-text 角色文字原子 (02-atoms / L2 实用原子)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2)
入口: @ze/css/02-atoms/index.css → @import
为什么有它:绝对严格分层后,L3 功能区 / L4 编辑器的 CSS 不准再直接用
var(--ze-fs-*) / var(--ze-ink-*) 写"次要标签 / 元数据小字 / 分组标题"。
把这几档高频角色文字封成 L2 原子,角色 = data-role 闭集,令牌消费收敛到本层。

## 对外表面

`.ze-rich`, `.ze-text`
