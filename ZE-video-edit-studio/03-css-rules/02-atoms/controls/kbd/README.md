# kbd

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

kbd.css — .ze-kbd 键帽原子(02-atoms / L2 视觉原子)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2)
入口: @ze/css/02-atoms/index.css → @import
用途:把「键盘按键」渲成带边框/微底的键帽样式(快捷键提示、富文本里的 <kbd> 标记)。
贴在 <kbd> 元素上即可:<kbd class="ze-kbd">⌘↵</kbd>。
字号用 0.85em(相对周围文字,键帽惯例自适应),不锁绝对档。

## 对外表面

`.ze-kbd`
