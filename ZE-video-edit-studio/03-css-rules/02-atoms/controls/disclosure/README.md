# disclosure

层级：L2 UI。代码入口：`code.css`。

这个对象不是散落 CSS 文件，而是一个可以被阅读、校验、扩展的模块。

## 说明

disclosure.css — ze-disclosure 折叠组头原子 (02-atoms)
协议: 03-css-rules/02-atoms/SPEC.md §2.2 / §3.1(折叠头≠按钮≠select)
入口: @ze/css/02-atoms/index.css → @import
层级编码:组头是"文字型"行(透明底/无边框/无 sunken 盒),静息必须比它治下的控件更轻;
标题排版由子节点 .ze-text[data-role=title] 供给 —— 统一标题配方仍只住
editor-shell.css 与 text.css 两处,本文件零复制。

## 对外表面

`.ze-disclosure`, `.ze-disclosure__caret`
