# popover

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

popover.css — ze-popover 组件视觉 (02-atoms) — CSS-only shell
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
视觉 DNA: 对齐 v3 header 的 menu-popover —— elev-2 深投影 + 1px overlay-soft 边 + lg 圆角。
data-placement 只画箭头朝向(视觉);**锚定定位算法 / 开合 = React 交互层(运行时),本批不做**。
popover 按内容定宽,无 data-size。

## 对外表面

`.ze-popover`, `.ze-popover__arrow`, `.ze-popover__body`, `.ze-popover__title`
