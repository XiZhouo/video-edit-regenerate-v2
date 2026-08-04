# tab

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

tab.css — ze-tab / ze-tabs 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
规范 tab 组件。**这是 LayoutTree TabContainer 也复用的同一套类**
(.ze-tabs / __headers / .ze-tab / __body)—— 故 04-editors/layout.css 不再
重复定义这些类(由本文件统一供给),LayoutRenderer 的 class 名不变即生效。

## 对外表面

`.ze-tab`, `.ze-tab__count`, `.ze-tabs`, `.ze-tabs__body`, `.ze-tabs__headers`
