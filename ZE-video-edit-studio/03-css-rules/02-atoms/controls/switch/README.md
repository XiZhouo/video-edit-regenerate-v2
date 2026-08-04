# switch

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

switch.css — ze-switch 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
实现: 原生 <input type=checkbox role=switch> + appearance:none 自定义轨道 + ::after 滑块。
off = overlay-soft 轨道;on = accent 轨道 + 滑块右移。
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-switch`, `.ze-switch__label`, `.ze-switch__row`
