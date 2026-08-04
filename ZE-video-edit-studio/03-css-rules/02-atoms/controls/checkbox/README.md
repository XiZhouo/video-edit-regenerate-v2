# checkbox

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

checkbox.css — ze-checkbox 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
实现: 原生 <input type=checkbox> 上 appearance:none 全自定义 + ::after 画 ✓。
不用 accent-color —— 紧凑尺度下 accent-color 跨浏览器盒子/圆角/focus 难控。
14px 盒 / 10px 勾是图标几何字面量(令牌层无此尺寸,对齐 v3 header 14px 图标先例)。

## 对外表面

`.ze-checkbox`, `.ze-checkbox__label`, `.ze-checkbox__row`
