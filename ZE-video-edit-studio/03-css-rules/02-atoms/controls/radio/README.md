# radio

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

radio.css — ze-radio 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
实现: 原生 <input type=radio> + appearance:none 全自定义 + ::after 实心点。
跟 checkbox 同密度(14px),圆形;选中 = accent 边 + accent 实心点。
全部走 令牌 (var(--ze-*));禁止字面量 hex;主题切换仅靠 token。

## 对外表面

`.ze-radio`, `.ze-radio__label`, `.ze-radio__row`
