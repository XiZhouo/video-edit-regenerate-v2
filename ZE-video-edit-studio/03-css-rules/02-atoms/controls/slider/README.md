# slider

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

slider.css — ze-slider 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
实现: 原生 <input type=range> + appearance:none,自定义轨道 + 拇指(thumb)。
轨道 overlay-soft;thumb accent 圆点。注:已填充进度的"左段着色"需 JS 设
background gradient,本批只做轨道 + thumb 基础视觉(纯 CSS 不依赖值)。

## 对外表面

`.ze-slider`
