# feedback

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

feedback.css — ze-spinner / ze-skeleton / ze-empty (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
三件反馈类合一,并集中托管共享 @keyframes(ze-spin / ze-skeleton-shimmer);
button.css 的 loading spinner 引用 ze-spin。带 prefers-reduced-motion guard。
empty 对齐 v3 footer 的空态(居中 icon + 文字)。

## 对外表面

`.ze-empty`, `.ze-empty__action`, `.ze-empty__body`, `.ze-empty__icon`, `.ze-empty__title`, `.ze-skeleton`, `.ze-spinner`
