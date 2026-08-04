# select

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

select.css — ze-select 组件视觉 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.3 / §2.4
入口: @ze/css/02-atoms/index.css → @import
结构: 外层 .ze-select(放 caret)包原生 <select class="ze-select__control">。
原生 select 不能挂 ::after,故 caret 由 wrapper 的 ::after 画(token 可着色,无 SVG 字面量)。
下拉 option 列表由 OS 渲染、CSS 管不到 —— 自定义 listbox 归 React 交互层 / menu-item,本批不做。

## 对外表面

`.ze-select`, `.ze-select__control`
