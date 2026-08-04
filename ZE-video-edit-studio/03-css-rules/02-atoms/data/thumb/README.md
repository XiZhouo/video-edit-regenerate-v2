# thumb

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

thumb.css — ze-thumb 缩略图网格组件原子 (02-atoms / L2 组件原子)
协议: 03-css-rules/02-atoms/SPEC.md · 总纲 ../../DESIGN-SYSTEM.md(§3 L2)
入口: @ze/css/02-atoms/index.css → @import
为什么在 L2:`.ze-thumb*` / `.ze-thumb-grid` / `.ze-marquee` 是框架级共享 primitive
(ThumbnailGrid / ThumbnailCard,被 gallery / asset / task 三编辑器复用 —— 见
01-core/.../primitives/ThumbnailGrid.tsx)。其卡片视觉天然是状态化的:选中 / 多选 /

## 对外表面

`.ze-icon-button`, `.ze-marquee`, `.ze-skeleton`, `.ze-thumb`, `.ze-thumb-grid`, `.ze-thumb__actions`, `.ze-thumb__description`, `.ze-thumb__img`, `.ze-thumb__label`, `.ze-thumb__loading`, `.ze-thumb__media`, `.ze-thumb__meta`, `.ze-thumb__placeholder`, `.ze-thumb__score`, `.ze-thumb__toggles`
