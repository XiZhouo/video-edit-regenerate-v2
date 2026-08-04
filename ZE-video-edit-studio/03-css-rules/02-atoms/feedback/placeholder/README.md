# placeholder

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

placeholder.css — ze-skeleton-lines / ze-progress-ring / ze-error-placeholder (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
Block1 加载 / 管线 / 占位系统的可复用视觉:
- skeleton-lines: 多行文本骨架容器(单行 .ze-skeleton 已在 feedback.css)
- progress-ring : 圆环进度(viewport / 缩略图叠加;0..1 或 indeterminate 旋转)

## 对外表面

`.ze-button`, `.ze-entity-busy`, `.ze-entity-frame`, `.ze-error-placeholder`, `.ze-error-placeholder__icon`, `.ze-error-placeholder__msg`, `.ze-progress-ring`, `.ze-progress-ring__fill`, `.ze-progress-ring__label`, `.ze-progress-ring__pct`, `.ze-progress-ring__track`, `.ze-skeleton-lines`
